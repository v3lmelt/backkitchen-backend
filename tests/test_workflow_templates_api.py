import copy

from app.models.album import Album
from app.models.circle import CircleMember
from app.models.workflow_template import WorkflowTemplate
from app.workflow_defaults import DEFAULT_WORKFLOW_CONFIG


def _create_circle(client, owner, auth_headers, name: str = "Circle One") -> int:
    response = client.post(
        "/api/circles",
        headers=auth_headers(owner),
        json={"name": name, "description": "desc"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _template_payload(name: str = "Template One") -> dict:
    return {
        "name": name,
        "description": f"{name} description",
        "workflow_config": copy.deepcopy(DEFAULT_WORKFLOW_CONFIG),
    }


def test_list_and_get_workflow_templates_require_circle_membership(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    member = factory.user(username="member")
    outsider = factory.user(username="outsider")
    circle_id = _create_circle(client, owner, auth_headers)

    create_response = client.post(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(owner),
        json=_template_payload(),
    )
    template_id = create_response.json()["id"]

    db_session.add(CircleMember(circle_id=circle_id, user_id=member.id, role="member"))
    db_session.commit()

    member_list = client.get(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(member),
    )
    member_detail = client.get(
        f"/api/circles/{circle_id}/workflow-templates/{template_id}",
        headers=auth_headers(member),
    )
    outsider_list = client.get(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(outsider),
    )

    assert member_list.status_code == 200
    assert [item["id"] for item in member_list.json()] == [template_id]
    assert member_detail.status_code == 200
    assert member_detail.json()["id"] == template_id
    assert member_detail.json()["album_count"] == 0
    assert outsider_list.status_code == 403


def test_only_circle_owner_can_manage_workflow_templates(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    member = factory.user(username="member")
    circle_id = _create_circle(client, owner, auth_headers)
    db_session.add(CircleMember(circle_id=circle_id, user_id=member.id, role="member"))
    db_session.commit()

    forbidden_create = client.post(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(member),
        json=_template_payload(),
    )
    assert forbidden_create.status_code == 403

    allowed_create = client.post(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(owner),
        json=_template_payload(),
    )
    assert allowed_create.status_code == 201
    template_id = allowed_create.json()["id"]

    forbidden_update = client.put(
        f"/api/circles/{circle_id}/workflow-templates/{template_id}",
        headers=auth_headers(member),
        json={"name": "Renamed by member"},
    )
    forbidden_delete = client.delete(
        f"/api/circles/{circle_id}/workflow-templates/{template_id}",
        headers=auth_headers(member),
    )

    assert forbidden_update.status_code == 403
    assert forbidden_delete.status_code == 403


def test_owner_can_update_workflow_template_and_get_album_count(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    circle_id = _create_circle(client, owner, auth_headers)

    created = client.post(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(owner),
        json=_template_payload(),
    )
    assert created.status_code == 201
    template_id = created.json()["id"]

    album = factory.album(producer=owner, mastering_engineer=mastering, members=[mastering], title="Album One")
    album.circle_id = circle_id
    album.workflow_template_id = template_id
    db_session.commit()

    updated_config = copy.deepcopy(DEFAULT_WORKFLOW_CONFIG)
    updated_config["steps"][0]["label"] = "Intake Review"
    update_response = client.put(
        f"/api/circles/{circle_id}/workflow-templates/{template_id}",
        headers=auth_headers(owner),
        json={
            "name": "Template Updated",
            "description": "Updated description",
            "workflow_config": updated_config,
        },
    )
    detail_response = client.get(
        f"/api/circles/{circle_id}/workflow-templates/{template_id}",
        headers=auth_headers(owner),
    )

    assert update_response.status_code == 200
    assert update_response.json()["name"] == "Template Updated"
    assert update_response.json()["workflow_config"]["steps"][0]["label"] == "Intake Review"
    assert detail_response.status_code == 200
    assert detail_response.json()["album_count"] == 1


def test_delete_workflow_template_clears_album_references(client, db_session, factory, auth_headers):
    owner = factory.user(role="producer")
    mastering = factory.user(username="mastering")
    circle_id = _create_circle(client, owner, auth_headers)

    created = client.post(
        f"/api/circles/{circle_id}/workflow-templates",
        headers=auth_headers(owner),
        json=_template_payload(),
    )
    template_id = created.json()["id"]

    album = factory.album(producer=owner, mastering_engineer=mastering, members=[mastering], title="Album One")
    album.circle_id = circle_id
    album.workflow_template_id = template_id
    db_session.commit()

    delete_response = client.delete(
        f"/api/circles/{circle_id}/workflow-templates/{template_id}",
        headers=auth_headers(owner),
    )

    db_session.expire_all()
    persisted_album = db_session.get(Album, album.id)
    deleted_template = db_session.get(WorkflowTemplate, template_id)

    assert delete_response.status_code == 204
    assert persisted_album is not None
    assert persisted_album.workflow_template_id is None
    assert deleted_template is None
