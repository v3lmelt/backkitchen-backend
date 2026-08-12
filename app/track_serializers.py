"""Track/issue/comment/event serialization helpers (ORM → API schemas)."""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.circle_permissions import is_album_manager
from app.mentions import build_mention_candidates
from app.models.album import Album
from app.models.checklist import ChecklistItem
from app.models.comment import Comment
from app.models.discussion import TrackDiscussion
from app.models.issue import Issue
from app.models.master_delivery import MasterDelivery
from app.models.stage_assignment import StageAssignment, StageAssignmentStatus
from app.models.track import Track
from app.models.track_composer import TrackExternalComposer
from app.models.track_source_version import TrackSourceVersion
from app.models.user import User
from app.models.workflow_event import WorkflowEvent
from app.schemas.schemas import (
    ChecklistItemRead,
    CommentAudioRead,
    CommentImageRead,
    CommentRead,
    DiscussionAudioRead,
    DiscussionImageRead,
    DiscussionRead,
    IssueAudioRead,
    IssueDetail,
    IssueImageRead,
    IssueMarkerRead,
    IssueRead,
    MasterDeliveryRead,
    TrackDetailResponse,
    TrackExternalComposerRead,
    TrackRead,
    TrackReviewStateRead,
    TrackSourceVersionRead,
    UserRead,
    WorkflowConfigSchema,
    WorkflowEventRead,
    WorkflowStepDefSchema,
    WorkflowTransitionOption,
)
from app.services.track_queries import (
    current_master_delivery,
    current_source_version,
    is_track_composer,
    is_track_composer_actor,
    pending_source_followup_request,
    track_composer_ordered_ids,
    track_external_composer_links,
    track_external_composer_names,
)
from app.track_permissions import (
    comment_visible_to_user,
    ensure_track_visibility,
    is_mastering_participant,
    issue_unresolved,
    issue_visible_to_user,
    mask_user_read_if_needed,
    peer_identity_anonymize_user_ids_for_viewer,
    should_anonymize_track,
    track_allowed_actions,
    user_read,
)
from app.workflow_engine import (
    classify_transition,
    get_allowed_transitions,
    get_current_step,
    parse_workflow_config,
    required_reviews_for_assignments,
    review_active_assignments,
    review_requires_group_finalization,
    target_is_mastering_related,
    user_matches_role,
    user_matches_role_or_assignment,
)

logger = logging.getLogger(__name__)

TRACK_DETAIL_DISCUSSION_SEED_SIZE = 20


def annotate_workflow_step_metadata(config_schema: WorkflowConfigSchema) -> WorkflowConfigSchema:
    """Populate server-computed per-step metadata (``is_mastering_related``).

    Lets clients stop hard-coding which steps belong to the mastering flow.
    """
    for step in config_schema.steps:
        step.is_mastering_related = target_is_mastering_related(step)
    return config_schema


def issue_audio_file_url(audio_id: int) -> str:
    """Return the authenticated API URL for an issue audio attachment."""
    return f"/api/issue-audios/{audio_id}/file"


def comment_audio_file_url(audio_id: int) -> str:
    """Return the authenticated API URL for a comment audio attachment."""
    return f"/api/comment-audios/{audio_id}/file"


def discussion_audio_file_url(audio_id: int) -> str:
    """Return the authenticated API URL for a discussion audio attachment."""
    return f"/api/discussion-audios/{audio_id}/file"


def track_external_composer_reads(
    track: Track,
    db: Session,
) -> list[TrackExternalComposerRead]:
    return [
        TrackExternalComposerRead.model_validate(link)
        for link in track_external_composer_links(track, db)
    ]


def track_composer_user_reads(
    db: Session,
    track: Track,
    anonymize_user_ids: set[int] | None = None,
) -> list[UserRead]:
    ordered_ids = track_composer_ordered_ids(track, db)
    if not ordered_ids:
        return []
    users_by_id = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(ordered_ids))).all()
    }
    result: list[UserRead] = []
    for user_id in ordered_ids:
        user = users_by_id.get(user_id)
        if user is None:
            continue
        masked_read = mask_user_read_if_needed(user_read(user), anonymize_user_ids)
        if masked_read is not None:
            result.append(masked_read)
    return result


def _source_version_read(version: TrackSourceVersion | None) -> TrackSourceVersionRead | None:
    if version is None:
        return None
    return TrackSourceVersionRead.model_validate(version)


def _master_delivery_read(delivery: MasterDelivery | None) -> MasterDeliveryRead | None:
    if delivery is None:
        return None
    return MasterDeliveryRead.model_validate(delivery)


def build_track_read(
    track: Track,
    user: User,
    album: Album,
    db: Session | None = None,
    *,
    anonymize: bool = False,
    anonymize_user_ids: set[int] | None = None,
    viewer_is_album_manager: bool | None = None,
) -> TrackRead:
    current_source = current_source_version(track)
    current_master = current_master_delivery(track)
    if anonymize_user_ids is None and db is not None:
        anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, user)
    visible_issues = [issue for issue in track.issues if issue_visible_to_user(issue, track, user)]
    open_issue_count = sum(1 for issue in visible_issues if issue_unresolved(issue))

    workflow_step = None
    workflow_transitions = None
    wf_config = parse_workflow_config(album)
    step = get_current_step(wf_config, track)
    if step:
        # model_validate (from_attributes) maps every StepDef field, so newly
        # added schema fields (e.g. revision_decision_policy) cannot be
        # silently dropped here.
        workflow_step = WorkflowStepDefSchema.model_validate(step)
        workflow_step.is_mastering_related = target_is_mastering_related(step)
    # Compute the active review assignments once and thread them through the
    # transition/action/assignee/review-state computations below.
    review_assignments: list[StageAssignment] | None = None
    if step is not None and step.type == "review" and db is not None:
        review_assignments = review_active_assignments(db, track.id, step.id)
    transitions = get_allowed_transitions(
        wf_config, track, user, album, db=db, review_assignments=review_assignments,
    )
    if transitions:
        workflow_transitions = []
        for t in transitions:
            kind, requires_confirmation = classify_transition(wf_config, track, t.target)
            workflow_transitions.append(WorkflowTransitionOption(
                decision=t.decision,
                label=t.label,
                kind=kind,
                requires_confirmation=requires_confirmation,
            ))
    external_names = [] if anonymize else track_external_composer_names(track, db)
    external_submitter_name = external_names[0] if external_names else track.external_submitter_name

    resolved_viewer_is_album_manager = (
        viewer_is_album_manager
        if viewer_is_album_manager is not None
        else is_album_manager(album, user, db) if db is not None else user.id == album.producer_id
    )

    if step is None:
        viewer_is_step_assignee = None
    elif db is not None:
        viewer_is_step_assignee = user_matches_role_or_assignment(
            user, album, track, step, db, review_assignments=review_assignments,
        )
    else:
        viewer_is_step_assignee = user_matches_role(user, album, track, step)

    review_state = None
    if step is not None and step.type == "review" and review_assignments is not None:
        completed_review_count = sum(
            1 for assignment in review_assignments
            if assignment.status == StageAssignmentStatus.COMPLETED
        )
        required_review_count = required_reviews_for_assignments(step, review_assignments)
        review_state = TrackReviewStateRead(
            step_id=step.id,
            assignment_mode=step.assignment_mode,
            required_review_count=required_review_count,
            active_assignment_count=len(review_assignments),
            completed_review_count=completed_review_count,
            quorum_reached=completed_review_count >= required_review_count,
            requires_group_finalization=review_requires_group_finalization(step, review_assignments),
        )

    return TrackRead(
        id=track.id,
        title=track.title,
        artist=None if anonymize else track.artist,
        album_id=track.album_id,
        album_checklist_enabled=album.checklist_enabled,
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
        composer_ids=track_composer_ordered_ids(track, db),
        external_composer_names=external_names,
        proxy_uploader_id=track.proxy_uploader_id,
        peer_reviewer_id=track.peer_reviewer_id,
        producer_id=album.producer_id,
        mastering_engineer_id=album.mastering_engineer_id,
        viewer_is_album_manager=resolved_viewer_is_album_manager,
        viewer_is_composer_actor=is_track_composer_actor(track, album, user.id, db),
        viewer_is_mastering_participant=is_mastering_participant(
            user, track, album, viewer_is_album_manager=resolved_viewer_is_album_manager,
        ),
        viewer_is_step_assignee=viewer_is_step_assignee,
        review_state=review_state,
        external_submitter_name=None if anonymize else external_submitter_name,
        is_proxy_submission=False if anonymize else bool(external_names and track.proxy_uploader_id),
        created_at=track.created_at,
        updated_at=track.updated_at,
        issue_count=len(visible_issues),
        open_issue_count=open_issue_count,
        submitter=(
            None
            if anonymize
            else mask_user_read_if_needed(user_read(track.submitter), anonymize_user_ids)
        ),
        composers=[] if anonymize or db is None else track_composer_user_reads(db, track, anonymize_user_ids),
        external_composers=[] if anonymize or db is None else track_external_composer_reads(track, db),
        proxy_uploader=(
            None
            if anonymize
            else mask_user_read_if_needed(user_read(track.proxy_uploader), anonymize_user_ids)
        ),
        peer_reviewer=(
            None
            if anonymize
            else mask_user_read_if_needed(user_read(track.peer_reviewer), anonymize_user_ids)
        ),
        current_source_version=_source_version_read(current_source),
        current_master_delivery=_master_delivery_read(current_master),
        pending_source_followup_request=(
            None if anonymize else pending_source_followup_request(db, track.id)
        ),
        allowed_actions=track_allowed_actions(
            track, user, album, _wf_config=wf_config, db=db, review_assignments=review_assignments,
        ),
        workflow_step=workflow_step,
        workflow_transitions=workflow_transitions,
        is_public=track.is_public,
        author_notes=track.author_notes,
        mastering_notes=track.mastering_notes,
        requested_revision_type=track.requested_revision_type,
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
            audio_url=issue_audio_file_url(audio.id),
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
            if comment_visible_to_user(comment, issue, viewer_track, viewer_user)
        )
    else:
        visible_comment_count = len(issue.comments)

    return IssueRead(
        id=issue.id,
        track_id=issue.track_id,
        local_number=issue.local_number,
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
        author=mask_user_read_if_needed(user_read(author), anonymize_user_ids),
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
            audio_url=comment_audio_file_url(audio.id),
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
        author=mask_user_read_if_needed(user_read(author), anonymize_user_ids),
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
        if comment_visible_to_user(comment, issue, track, user)
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
        actor=mask_user_read_if_needed(user_read(actor), anonymize_user_ids),
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
            selectinload(Track.source_versions),
            selectinload(Track.master_deliveries),
            selectinload(Track.checklist_items),
            selectinload(Track.composer_links),
            selectinload(Track.external_composer_links),
            selectinload(Track.submitter),
            selectinload(Track.proxy_uploader),
            selectinload(Track.peer_reviewer),
        )
    )

    # Seed the discussion panel with the most recent slice; the wall view will
    # paginate older entries on demand via /api/tracks/{id}/discussions.
    can_see_mastering = is_mastering_participant(user, track, album)
    suppress_internal = is_track_composer(track, user.id, db) and user.id != album.producer_id
    recent_discussions_stmt = (
        select(TrackDiscussion)
        .where(TrackDiscussion.track_id == track.id)
        .options(
            selectinload(TrackDiscussion.images),
            selectinload(TrackDiscussion.audios),
        )
        .order_by(TrackDiscussion.id.desc())
        .limit(TRACK_DETAIL_DISCUSSION_SEED_SIZE)
    )
    if not can_see_mastering:
        recent_discussions_stmt = recent_discussions_stmt.where(TrackDiscussion.phase != "mastering")
    if suppress_internal:
        recent_discussions_stmt = recent_discussions_stmt.where(TrackDiscussion.visibility != "internal")
    recent_discussions = list(reversed(db.scalars(recent_discussions_stmt).all()))

    # Pre-fetch all user IDs we'll need to avoid N+1
    user_ids: set[int] = set()
    for issue in track.issues:
        user_ids.add(issue.author_id)
        for comment in issue.comments:
            user_ids.add(comment.author_id)
    for event in track.workflow_events:
        if event.actor_user_id:
            user_ids.add(event.actor_user_id)
    for d in recent_discussions:
        user_ids.add(d.author_id)
    user_ids.discard(None)

    users_by_id: dict[int, User] = {}
    if user_ids:
        fetched = db.scalars(select(User).where(User.id.in_(user_ids))).all()
        users_by_id = {u.id: u for u in fetched}

    source_version_numbers = {version.id: version.version_number for version in track.source_versions}
    anonymize_user_ids = peer_identity_anonymize_user_ids_for_viewer(db, track, album, user)
    visible_issues = [issue for issue in track.issues if issue_visible_to_user(issue, track, user)]
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
            phase=d.phase,
            content=d.content,
            created_at=d.created_at,
            edited_at=d.edited_at,
            author=mask_user_read_if_needed(
                user_read(users_by_id.get(d.author_id) or d.author),
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
            audios=[
                DiscussionAudioRead(
                    id=a.id,
                    discussion_id=a.discussion_id,
                    audio_url=discussion_audio_file_url(a.id),
                    original_filename=a.original_filename,
                    duration=a.duration,
                    created_at=a.created_at,
                )
                for a in d.audios
            ],
        )
        for d in recent_discussions
    ]
    # Mirror the defensive try/except used in `_album_to_read`: a stored
    # config that no longer passes the current validator must not crash the
    # entire track-detail read. Callers that need a valid config (workflow
    # transitions, step view) will either skip gracefully or surface a
    # targeted 4xx.
    wf_config_schema = None
    try:
        wf_config_schema = annotate_workflow_step_metadata(
            WorkflowConfigSchema(**parse_workflow_config(album))
        )
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
        mention_candidates=build_mention_candidates(
            db,
            track,
            album,
            user,
            anonymize_user_ids=anonymize_user_ids,
        ),
    )
