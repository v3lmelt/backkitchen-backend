import json
import logging
from typing import Any

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.album import Album
from app.models.album_member import AlbumMember
from app.models.checklist import ChecklistItem
from app.models.comment import Comment
from app.models.comment_audio import CommentAudio
from app.models.comment_image import CommentImage
from app.models.issue import Issue, IssueStatus
from app.models.discussion import TrackDiscussion
from app.models.master_delivery import MasterDelivery
from app.models.stage_assignment import StageAssignment
from app.models.track import Track, TrackStatus
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.schemas.schemas import (
    AlbumMemberRead,
    ChecklistItemRead,
    CommentAudioRead,
    CommentImageRead,
    CommentRead,
    DiscussionImageRead,
    DiscussionRead,
    IssueAudioRead,
    IssueDetail,
    IssueImageRead,
    IssueMarkerRead,
    IssueRead,
    MasterDeliveryRead,
    TrackDetailResponse,
    TrackRead,
    TrackSourceVersionRead,
    UserRead,
    WorkflowEventRead,
)


def _audio_url(audio) -> str:
    """Return the public URL for an audio attachment.

    For R2-stored files, returns the public CDN URL directly.
    For local files, returns the ``/uploads/`` path.
    """
    if audio.storage_backend == "r2":
        from app.services.r2 import public_url

        return public_url(audio.file_path)
    return f"/uploads/{audio.file_path}"


def get_album_member_ids(db: Session, album_id: int) -> set[int]:
    rows = db.scalars(select(AlbumMember).where(AlbumMember.album_id == album_id)).all()
    return {row.user_id for row in rows}


def get_all_album_member_ids(db: Session, album_id: int | None = None) -> dict[int, set[int]]:
    """Batch-fetch album→member mappings in a single query.

    When ``album_id`` is provided only that album's members are loaded,
    avoiding a full-table scan when the caller only needs one album.
    """
    stmt = select(AlbumMember)
    if album_id is not None:
        stmt = stmt.where(AlbumMember.album_id == album_id)
    result: dict[int, set[int]] = {}
    for m in db.scalars(stmt).all():
        result.setdefault(m.album_id, set()).add(m.user_id)
    return result


def ensure_album_producer(album_id: int, user: User, db: Session) -> Album:
    album = db.get(Album, album_id)
    if album is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Album not found.",
        )
    if album.producer_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the album producer can perform this action.",
        )
    return album


def ensure_album_visibility(album: Album, user: User, db: Session) -> None:
    member_ids = get_album_member_ids(db, album.id)
    visible_ids = {album.producer_id, album.mastering_engineer_id}
    visible_ids.update(member_ids)
    if user.id not in visible_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this album.",
        )


def is_album_completed(db: Session, album_id: int) -> bool:
    """True when every active (non-archived, non-rejected) track is completed."""
    total, completed = db.execute(
        select(
            func.count(Track.id),
            func.count(case((Track.status == TrackStatus.COMPLETED, Track.id))),
        ).where(
            Track.album_id == album_id,
            Track.archived_at.is_(None),
            Track.status != TrackStatus.REJECTED,
        )
    ).one()
    return total > 0 and total == completed


def should_anonymize_track(track: Track, user: User, album: Album) -> bool:
    """Return True if the user should see an anonymized view of this track.

    Full info is shown to: the album producer, the mastering engineer, and the
    track's own submitter.  Everyone else sees artist/submitter redacted.
    """
    if user.id in (album.producer_id, album.mastering_engineer_id):
        return False
    if user.id == track.submitter_id:
        return False
    return True


def _is_identity_privileged_viewer(user: User, album: Album) -> bool:
    return user.id in (album.producer_id, album.mastering_engineer_id)


def _hash_user_id(user_id: int) -> str:
    h = 2166136261
    for char in str(user_id):
        h ^= ord(char)
        h = (h * 16777619) & 0xFFFFFFFF
    return f"{h:08X}"[:6]


def _masked_user_read(user_read: UserRead) -> UserRead:
    token = _hash_user_id(user_read.id)
    return user_read.model_copy(
        update={
            "username": f"anon_{token.lower()}",
            "display_name": f"#{token}",
            "email": None,
        }
    )


def _is_peer_identity_anonymous_phase(track: Track, album: Album) -> bool:
    if track.status in {"peer_review", "peer_revision"}:
        return True

    try:
        from app.workflow_engine import (
            get_current_step,
            get_step_by_id,
            get_steps,
            infer_issue_phase_for_step,
            parse_workflow_config,
        )

        config = parse_workflow_config(album)
        step = get_current_step(config, track)
        if step is None:
            return False
        if infer_issue_phase_for_step(step) == "peer":
            return True
        if step.type == "revision" and step.return_to:
            return_to = get_step_by_id(get_steps(config), step.return_to)
            return bool(return_to and infer_issue_phase_for_step(return_to) == "peer")
    except Exception:
        return False

    return False


def _peer_identity_user_ids(db: Session, track: Track, album: Album) -> set[int]:
    user_ids = {track.submitter_id, track.peer_reviewer_id}

    stage_ids: list[str] = []
    try:
        from app.workflow_engine import get_steps, infer_issue_phase_for_step, parse_workflow_config

        config = parse_workflow_config(album)
        stage_ids = [
            step.id
            for step in get_steps(config)
            if step.type == "review" and infer_issue_phase_for_step(step) == "peer"
        ]
    except Exception:
        stage_ids = []

    if not stage_ids:
        stage_ids = ["peer_review"]

    reviewer_ids = db.scalars(
        select(StageAssignment.user_id).where(
            StageAssignment.track_id == track.id,
            StageAssignment.stage_id.in_(stage_ids),
        )
    ).all()
    user_ids.update(reviewer_ids)
    user_ids.discard(None)
    return {uid for uid in user_ids if uid is not None}


def peer_identity_anonymize_user_ids_for_viewer(
    db: Session,
    track: Track,
    album: Album,
    viewer: User,
) -> set[int]:
    if _is_identity_privileged_viewer(viewer, album):
        return set()
    if not _is_peer_identity_anonymous_phase(track, album):
        return set()
    user_ids = _peer_identity_user_ids(db, track, album)
    user_ids.discard(viewer.id)
    return user_ids


def ensure_track_visibility(track: Track, user: User, db: Session) -> Album:
    album = db.get(Album, track.album_id)
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    # Producer and mastering engineer always have access
    if user.id in (album.producer_id, album.mastering_engineer_id):
        return album
    # Submitter and peer reviewer of this specific track always have access
    if user.id == track.submitter_id or user.id == track.peer_reviewer_id:
        return album
    assignment_id = db.scalar(
        select(StageAssignment.id).where(
            StageAssignment.track_id == track.id,
            StageAssignment.user_id == user.id,
            StageAssignment.status.in_(["pending", "completed"]),
        )
    )
    if assignment_id is not None:
        return album
    # Must be an album member
    member_ids = get_album_member_ids(db, album.id)
    if user.id not in member_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this album.",
        )
    # Regular members can only see tracks that are completed or marked public
    if track.status != TrackStatus.COMPLETED and not track.is_public:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this track.",
        )
    return album


def current_source_version(track: Track) -> TrackSourceVersion | None:
    if not track.source_versions:
        return None
    return max(track.source_versions, key=lambda item: item.version_number)


def current_master_delivery(track: Track) -> MasterDelivery | None:
    if not track.master_deliveries:
        return None
    current_cycle_deliveries = [
        item for item in track.master_deliveries if item.workflow_cycle == track.workflow_cycle
    ]
    if not current_cycle_deliveries:
        return None
    return max(current_cycle_deliveries, key=lambda item: item.delivery_number)


def track_allowed_actions(
    track: Track, user: User, album: Album, *,
    _wf_config: dict | None = None, db: Session | None = None,
) -> list[str]:
    from app.workflow_engine import get_allowed_action_names, parse_workflow_config

    config = _wf_config or parse_workflow_config(album)
    return get_allowed_action_names(config, track, user, album, db=db)


def _user_read(user: User | None) -> UserRead | None:
    if user is None:
        return None
    return UserRead.model_validate(user)


def _mask_user_read_if_needed(
    user_read: UserRead | None,
    anonymize_user_ids: set[int] | None,
) -> UserRead | None:
    if user_read is None:
        return None
    if anonymize_user_ids and user_read.id in anonymize_user_ids:
        return _masked_user_read(user_read)
    return user_read


def mask_user_read_if_needed(
    user_read: UserRead | None,
    anonymize_user_ids: set[int] | None,
) -> UserRead | None:
    return _mask_user_read_if_needed(user_read, anonymize_user_ids)


def _source_version_read(version: TrackSourceVersion | None) -> TrackSourceVersionRead | None:
    if version is None:
        return None
    return TrackSourceVersionRead.model_validate(version)


def _master_delivery_read(delivery: MasterDelivery | None) -> MasterDeliveryRead | None:
    if delivery is None:
        return None
    return MasterDeliveryRead.model_validate(delivery)


def _issue_visible_to_user(issue: Issue, track: Track, user: User) -> bool:
    return not (
        user.id == track.submitter_id
        and issue.status in {IssueStatus.PENDING_DISCUSSION, IssueStatus.INTERNAL_RESOLVED}
    )


def _comment_visible_to_user(comment: Comment, issue: Issue, track: Track, user: User) -> bool:
    album = track.album
    if album is not None and user.id == track.submitter_id and user.id == album.producer_id:
        return _issue_visible_to_user(issue, track, user)
    if comment.visibility == "internal" and user.id == track.submitter_id:
        return False
    return _issue_visible_to_user(issue, track, user)


def _issue_unresolved(issue: Issue) -> bool:
    return issue.status in {
        IssueStatus.OPEN,
        IssueStatus.PENDING_DISCUSSION,
    }


def build_track_read(
    track: Track,
    user: User,
    album: Album,
    db: Session | None = None,
    *,
    anonymize: bool = False,
    anonymize_user_ids: set[int] | None = None,
) -> TrackRead:
    from app.workflow_engine import (
        get_allowed_transitions,
        get_current_step,
        parse_workflow_config,
    )
    from app.schemas.schemas import WorkflowStepDefSchema

    current_source = current_source_version(track)
    current_master = current_master_delivery(track)
    if anonymize_user_ids is None and db is not None:
        anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, user)
    visible_issues = [issue for issue in track.issues if _issue_visible_to_user(issue, track, user)]
    open_issue_count = sum(1 for issue in visible_issues if _issue_unresolved(issue))

    workflow_step = None
    workflow_transitions = None
    wf_config = parse_workflow_config(album)
    step = get_current_step(wf_config, track)
    if step:
        workflow_step = WorkflowStepDefSchema(
            id=step.id,
            label=step.label,
            type=step.type,
            ui_variant=step.ui_variant,
            assignee_role=step.assignee_role,
            order=step.order,
            transitions=step.transitions,
            return_to=step.return_to,
            revision_step=step.revision_step,
            allow_permanent_reject=step.allow_permanent_reject,
            assignment_mode=step.assignment_mode,
            reviewer_pool=step.reviewer_pool,
            required_reviewer_count=step.required_reviewer_count,
            assignee_user_id=step.assignee_user_id,
            require_confirmation=step.require_confirmation,
            actor_roles=step.actor_roles,
        )
    transitions = get_allowed_transitions(wf_config, track, user, album, db=db)
    if transitions:
        workflow_transitions = [
            {"decision": t.decision, "label": t.label} for t in transitions
        ]

    return TrackRead(
        id=track.id,
        title=track.title,
        artist=None if anonymize else track.artist,
        album_id=track.album_id,
        bpm=track.bpm,
        original_title=track.original_title,
        original_artist=track.original_artist,
        track_number=track.track_number,
        file_path=track.file_path,
        duration=track.duration,
        status=track.status,
        rejection_mode=track.rejection_mode,
        workflow_variant=track.workflow_variant or "standard",
        version=track.version,
        workflow_cycle=track.workflow_cycle,
        submitter_id=track.submitter_id,
        peer_reviewer_id=track.peer_reviewer_id,
        producer_id=album.producer_id,
        mastering_engineer_id=album.mastering_engineer_id,
        created_at=track.created_at,
        updated_at=track.updated_at,
        issue_count=len(visible_issues),
        open_issue_count=open_issue_count,
        submitter=(
            None
            if anonymize
            else _mask_user_read_if_needed(_user_read(track.submitter), anonymize_user_ids)
        ),
        peer_reviewer=(
            None
            if anonymize
            else _mask_user_read_if_needed(_user_read(track.peer_reviewer), anonymize_user_ids)
        ),
        current_source_version=_source_version_read(current_source),
        current_master_delivery=_master_delivery_read(current_master),
        allowed_actions=track_allowed_actions(track, user, album, _wf_config=wf_config, db=db),
        workflow_step=workflow_step,
        workflow_transitions=workflow_transitions,
        is_public=track.is_public,
        author_notes=track.author_notes,
        mastering_notes=track.mastering_notes,
    )


def build_issue_read(
    issue: Issue,
    db: Session,
    source_version_numbers: dict[int, int] | None = None,
    users_cache: dict[int, User] | None = None,
    anonymize_user_ids: set[int] | None = None,
    *,
    viewer_user: User | None = None,
    viewer_track: Track | None = None,
) -> IssueRead:
    if users_cache is not None:
        author = users_cache.get(issue.author_id) or db.get(User, issue.author_id)
    else:
        author = db.get(User, issue.author_id)
    source_version_number = None
    if issue.source_version_id:
        if source_version_numbers is not None:
            source_version_number = source_version_numbers.get(issue.source_version_id)
        else:
            source_version = db.get(TrackSourceVersion, issue.source_version_id)
            source_version_number = source_version.version_number if source_version else None
    markers = [IssueMarkerRead.model_validate(m) for m in issue.markers]
    audios = [
        IssueAudioRead(
            id=audio.id,
            issue_id=audio.issue_id,
            audio_url=_audio_url(audio),
            original_filename=audio.original_filename,
            duration=audio.duration,
            created_at=audio.created_at,
        )
        for audio in issue.audios
    ]
    images = [
        IssueImageRead(
            id=image.id,
            issue_id=image.issue_id,
            image_url=f"/uploads/{image.file_path}",
            created_at=image.created_at,
        )
        for image in issue.images
    ]
    if viewer_user is not None and viewer_track is not None:
        visible_comment_count = sum(
            1
            for comment in issue.comments
            if _comment_visible_to_user(comment, issue, viewer_track, viewer_user)
        )
    else:
        visible_comment_count = len(issue.comments)

    return IssueRead(
        id=issue.id,
        track_id=issue.track_id,
        author_id=issue.author_id,
        phase=issue.phase,
        workflow_cycle=issue.workflow_cycle,
        source_version_id=issue.source_version_id,
        source_version_number=source_version_number,
        master_delivery_id=issue.master_delivery_id,
        title=issue.title,
        description=issue.description,
        severity=issue.severity,
        status=issue.status,
        markers=markers,
        audios=audios,
        images=images,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        comment_count=visible_comment_count,
        author=_mask_user_read_if_needed(_user_read(author), anonymize_user_ids),
    )


def build_comment_read(
    comment: Comment,
    db: Session,
    users_cache: dict[int, User] | None = None,
    anonymize_user_ids: set[int] | None = None,
) -> CommentRead:
    if users_cache and comment.author_id in users_cache:
        author = users_cache[comment.author_id]
    else:
        author = db.get(User, comment.author_id)
    images = [
        CommentImageRead(
            id=image.id,
            comment_id=image.comment_id,
            image_url=f"/uploads/{image.file_path}",
            created_at=image.created_at,
        )
        for image in comment.images
    ]
    audios = [
        CommentAudioRead(
            id=audio.id,
            comment_id=audio.comment_id,
            audio_url=_audio_url(audio),
            original_filename=audio.original_filename,
            duration=audio.duration,
            created_at=audio.created_at,
        )
        for audio in comment.audios
    ]
    return CommentRead(
        id=comment.id,
        issue_id=comment.issue_id,
        author_id=comment.author_id,
        content=comment.content,
        visibility=comment.visibility,
        is_status_note=comment.is_status_note,
        old_status=comment.old_status,
        new_status=comment.new_status,
        created_at=comment.created_at,
        author=_mask_user_read_if_needed(_user_read(author), anonymize_user_ids),
        images=images,
        audios=audios,
    )


def build_issue_detail(
    issue: Issue,
    db: Session,
    *,
    anonymize_user_ids: set[int] | None = None,
) -> IssueDetail:
    # Pre-fetch users for this issue's comments
    user_ids = {issue.author_id} | {c.author_id for c in issue.comments}
    users_by_id = {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()}
    issue_read = build_issue_read(
        issue,
        db,
        users_cache=users_by_id,
        anonymize_user_ids=anonymize_user_ids,
    )
    comments = [
        build_comment_read(
            comment,
            db,
            users_cache=users_by_id,
            anonymize_user_ids=anonymize_user_ids,
        )
        for comment in issue.comments
    ]
    return IssueDetail(**issue_read.model_dump(), comments=comments)


def build_issue_detail_for_user(issue: Issue, track: Track, user: User, db: Session) -> IssueDetail:
    album = db.get(Album, track.album_id)
    anonymize_user_ids: set[int] = set()
    if album is not None:
        anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, user)

    user_ids = {issue.author_id} | {c.author_id for c in issue.comments}
    users_by_id = {u.id: u for u in db.scalars(select(User).where(User.id.in_(user_ids))).all()}
    issue_read = build_issue_read(
        issue,
        db,
        users_cache=users_by_id,
        anonymize_user_ids=anonymize_user_ids,
        viewer_user=user,
        viewer_track=track,
    )
    comments = [
        build_comment_read(
            comment,
            db,
            users_cache=users_by_id,
            anonymize_user_ids=anonymize_user_ids,
        )
        for comment in issue.comments
        if _comment_visible_to_user(comment, issue, track, user)
    ]
    return IssueDetail(**issue_read.model_dump(), comments=comments)


def build_checklist_read(item: ChecklistItem) -> ChecklistItemRead:
    return ChecklistItemRead.model_validate(item)


def build_event_read(
    event: WorkflowEvent,
    db: Session,
    users_cache: dict[int, User] | None = None,
    anonymize_user_ids: set[int] | None = None,
) -> WorkflowEventRead:
    if users_cache and event.actor_user_id and event.actor_user_id in users_cache:
        actor = users_cache[event.actor_user_id]
    elif event.actor_user_id:
        actor = db.get(User, event.actor_user_id)
    else:
        actor = None
    payload = json.loads(event.payload) if event.payload else None
    return WorkflowEventRead(
        id=event.id,
        event_type=event.event_type,
        from_status=event.from_status,
        to_status=event.to_status,
        payload=payload,
        created_at=event.created_at,
        actor=_mask_user_read_if_needed(_user_read(actor), anonymize_user_ids),
    )


def build_track_detail(track: Track, user: User, db: Session) -> TrackDetailResponse:
    album = ensure_track_visibility(track, user, db)

    # Re-fetch the track with all relationships eagerly loaded to avoid N+1
    track = db.scalar(
        select(Track)
        .where(Track.id == track.id)
        .options(
            selectinload(Track.issues).selectinload(Issue.markers),
            selectinload(Track.issues).selectinload(Issue.audios),
            selectinload(Track.issues).selectinload(Issue.images),
            selectinload(Track.issues).selectinload(Issue.comments).selectinload(Comment.images),
            selectinload(Track.issues).selectinload(Issue.comments).selectinload(Comment.audios),
            selectinload(Track.workflow_events),
            selectinload(Track.discussions).selectinload(TrackDiscussion.images),
            selectinload(Track.source_versions),
            selectinload(Track.master_deliveries),
            selectinload(Track.checklist_items),
            selectinload(Track.submitter),
            selectinload(Track.peer_reviewer),
        )
    )

    # Pre-fetch all user IDs we'll need to avoid N+1
    user_ids: set[int] = set()
    for issue in track.issues:
        user_ids.add(issue.author_id)
        for comment in issue.comments:
            user_ids.add(comment.author_id)
    for event in track.workflow_events:
        if event.actor_user_id:
            user_ids.add(event.actor_user_id)
    for d in track.discussions:
        user_ids.add(d.author_id)
    user_ids.discard(None)

    users_by_id: dict[int, User] = {}
    if user_ids:
        fetched = db.scalars(select(User).where(User.id.in_(user_ids))).all()
        users_by_id = {u.id: u for u in fetched}

    source_version_numbers = {version.id: version.version_number for version in track.source_versions}
    anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, user)
    visible_issues = [issue for issue in track.issues if _issue_visible_to_user(issue, track, user)]
    issues = [
        build_issue_read(
            issue,
            db,
            source_version_numbers,
            users_by_id,
            anonymize_user_ids,
            viewer_user=user,
            viewer_track=track,
        )
        for issue in sorted(visible_issues, key=lambda row: (row.created_at, row.id))
    ]
    current_source = current_source_version(track)
    checklist_items = [
        build_checklist_read(item)
        for item in track.checklist_items
        if current_source is None or item.source_version_id == current_source.id
    ]
    events = [
        build_event_read(
            event,
            db,
            users_by_id,
            anonymize_user_ids,
        )
        for event in track.workflow_events
    ]
    discussions = [
        DiscussionRead(
            id=d.id,
            track_id=d.track_id,
            author_id=d.author_id,
            visibility=d.visibility,
            content=d.content,
            created_at=d.created_at,
            author=_mask_user_read_if_needed(
                _user_read(users_by_id.get(d.author_id) or d.author),
                anonymize_user_ids,
            ),
            images=[
                DiscussionImageRead(
                    id=img.id,
                    discussion_id=img.discussion_id,
                    image_url=f"/uploads/{img.file_path}",
                    created_at=img.created_at,
                )
                for img in d.images
            ],
        )
        for d in track.discussions
        if not (
            d.visibility == "internal"
            and user.id == track.submitter_id
            and user.id != album.producer_id
        )
    ]
    # Mirror the defensive try/except used in `_album_to_read`: a stored
    # config that no longer passes the current validator must not crash the
    # entire track-detail read. Callers that need a valid config (workflow
    # transitions, step view) will either skip gracefully or surface a
    # targeted 4xx.
    from app.workflow_engine import parse_workflow_config
    from app.schemas.schemas import WorkflowConfigSchema
    wf_config_schema = None
    try:
        wf_config_schema = WorkflowConfigSchema(**parse_workflow_config(album))
    except Exception:
        logger.warning(
            "Album %d has an invalid workflow_config; track-detail will return it as None.",
            album.id,
        )

    anonymize = should_anonymize_track(track, user, album)
    return TrackDetailResponse(
        track=build_track_read(
            track,
            user,
            album,
            db=db,
            anonymize=anonymize,
            anonymize_user_ids=anonymize_user_ids,
        ),
        issues=issues,
        checklist_items=checklist_items,
        events=events,
        source_versions=[TrackSourceVersionRead.model_validate(v) for v in track.source_versions],
        master_deliveries=[MasterDeliveryRead.model_validate(d) for d in track.master_deliveries],
        discussions=discussions,
        workflow_config=wf_config_schema,
    )


def log_track_event(
    db: Session,
    track: Track,
    actor: User | None,
    event_type: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> WorkflowEvent:
    def _serialize(value: Any) -> Any:
        if hasattr(value, "value"):
            return value.value
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    event = WorkflowEvent(
        track_id=track.id,
        album_id=track.album_id,
        actor_user_id=actor.id if actor else None,
        event_type=event_type,
        from_status=from_status.value if hasattr(from_status, "value") else from_status,
        to_status=to_status.value if hasattr(to_status, "value") else to_status,
        payload=json.dumps(payload, default=_serialize) if payload else None,
    )
    db.add(event)
    return event


