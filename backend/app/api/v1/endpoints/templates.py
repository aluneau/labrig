"""Customer-case templates (docs/templates.md)"""
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.template import (
    TemplateCreated, TemplateDetail, TemplateList, TemplateRender, TemplateRenderRequest, TemplateSave,
)
from app.services.template_service import TemplateError, template_service

router = APIRouter()


def _call(fn, *args):
    try:
        return fn(*args)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=TemplateList)
def list_templates():
    """Built-in + user templates; files that fail to load are listed in `errors`"""
    return template_service.list()


@router.post("", response_model=TemplateDetail, status_code=201)
def save_group_as_template(body: TemplateSave, db: Session = Depends(get_db)):
    """Save an existing group as a user template (DATA_DIR/templates/<id>.yaml): assigned fields are
    stripped, addresses of the group network become {{ip:N}}, the name and subnet become parameters"""
    return _call(template_service.save_from_group, db, body)


@router.post("/check")
def check_template(body: Dict[str, Any] = Body(..., examples=[{"yaml": "id: my-lab\n..."}])):
    """Validate a template document (YAML text in `yaml`) without saving it"""
    try:
        data = template_service.parse(str(body.get("yaml") or ""))
    except (TemplateError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "id": data.id, "title": data.title}


@router.get("/{template_id}", response_model=TemplateDetail)
def get_template(template_id: str):
    return _call(lambda: template_service.get(template_id).detail())


@router.delete("/{template_id}")
def delete_template(template_id: str):
    """Delete a user template (built-in ones are read-only)"""
    _call(template_service.delete, template_id)
    return {"message": f"Template {template_id} deleted"}


@router.post("/{template_id}/render", response_model=TemplateRender)
def render_template(template_id: str, body: TemplateRenderRequest, db: Session = Depends(get_db)):
    """Preview: the final spec (JSON + YAML + OpenTofu), errors (invalid values, conflicts on this host)
    and the resource estimate against the host's free RAM. Nothing is created."""
    return _call(template_service.render, db, template_id, body.params, body.yaml)


@router.post("/{template_id}/create", response_model=TemplateCreated, status_code=202)
def create_from_template(template_id: str, body: TemplateRenderRequest, db: Session = Depends(get_db)):
    """Create the lab group (task) and, if the template has one, a task that creates the cluster in it
    once the group is ready. The group keeps the template id, case number, parameters and guide."""
    return _call(template_service.create, db, template_id, body.params, body.yaml)
