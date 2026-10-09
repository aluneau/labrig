"""Customer-case templates (docs/templates.md)

A template is a YAML file: metadata shown in the gallery, typed parameters, a GroupSpec (+ an optional
cluster request) with `{{param}}` placeholders, and a markdown guide of what to reproduce / check.
"""
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

TEMPLATE_ID = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"
PARAM_NAME = r"^[a-z_][a-z0-9_]{0,31}$"
PARAM_TYPES = ("string", "int", "bool", "choice", "cloud_image", "cidr")


class TemplateParam(BaseModel):
    name: str = Field(..., pattern=PARAM_NAME)
    label: Optional[str] = None
    type: Literal["string", "int", "bool", "choice", "cloud_image", "cidr"] = "string"
    required: bool = False
    default: Any = None
    help: Optional[str] = None
    pattern: Optional[str] = None       # string: full-match regex
    choices: List[Any] = []             # choice: allowed values
    min: Optional[int] = None           # int bounds
    max: Optional[int] = None

    @model_validator(mode="after")
    def _check(self):
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"param {self.name}: bad pattern ({e})")
        if self.type == "choice" and not self.choices:
            raise ValueError(f"param {self.name}: type choice needs 'choices'")
        if self.type != "choice" and self.choices:
            raise ValueError(f"param {self.name}: 'choices' is only for type choice")
        if self.default is not None:
            if self.type == "int" and (isinstance(self.default, bool) or not isinstance(self.default, int)):
                raise ValueError(f"param {self.name}: default must be an integer")
            if self.type == "bool" and not isinstance(self.default, bool):
                raise ValueError(f"param {self.name}: default must be true or false")
            if self.type == "choice" and self.default not in self.choices:
                raise ValueError(f"param {self.name}: default {self.default!r} is not one of the choices")
        return self


class TemplateResources(BaseModel):
    """Estimate shown on the card (the whole lab: router + members + cluster nodes)"""
    vcpus: int = Field(0, ge=0)
    memory_mb: int = Field(0, ge=0)
    disk_gb: int = Field(0, ge=0)


class TemplateFile(BaseModel):
    """The YAML file as written (before rendering)"""
    id: str = Field(..., pattern=TEMPLATE_ID)
    title: str = Field(..., min_length=1, max_length=120)
    summary: str = Field("", max_length=500)
    tags: List[str] = []
    requires: List[str] = []
    resources: TemplateResources = TemplateResources()
    params: List[TemplateParam] = []
    name: str = Field(..., min_length=1)       # group name, with placeholders
    group: Dict[str, Any]
    cluster: Optional[Dict[str, Any]] = None
    guide: str = ""
    # Extensions (docs/templates.md)
    heavy: bool = False                       # needs a lot of RAM / downloads: the card says so
    author: Optional[str] = None

    @field_validator("params")
    @classmethod
    def _params(cls, v: List[TemplateParam]) -> List[TemplateParam]:
        names = [p.name for p in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"params listed twice: {', '.join(sorted(dupes))}")
        if "case" not in names:
            raise ValueError("params must include 'case' (the case number)")
        reserved = {"group", "domain"} & set(names)
        if reserved:
            raise ValueError(f"param names reserved for built-ins: {', '.join(sorted(reserved))}")
        return v


# API

class TemplateInfo(BaseModel):
    id: str
    title: str
    summary: str = ""
    tags: List[str] = []
    requires: List[str] = []
    resources: TemplateResources = TemplateResources()
    params: List[TemplateParam] = []
    heavy: bool = False
    has_cluster: bool = False
    cluster_type: Optional[str] = None
    custom: bool = False                 # user template (DATA_DIR/templates), deletable
    author: Optional[str] = None


class TemplateDetail(TemplateInfo):
    name: str
    group: Dict[str, Any]
    cluster: Optional[Dict[str, Any]] = None
    guide: str = ""
    yaml: str = ""                        # the file as stored


class TemplateLoadError(BaseModel):
    file: str
    error: str


class TemplateList(BaseModel):
    templates: List[TemplateInfo] = []
    errors: List[TemplateLoadError] = []  # files that failed to load (not shown as cards)


class TemplateRenderRequest(BaseModel):
    params: Dict[str, Any] = {}
    # The reviewed document (YAML with `group:` and optional `cluster:`), edited in the wizard:
    # replaces the rendered one
    yaml: Optional[str] = Field(None, max_length=200000)


class TemplateEstimate(BaseModel):
    vcpus: int = 0
    memory_mb: int = 0
    disk_gb: int = 0
    host_memory_free_mb: Optional[int] = None
    fits: bool = True
    detail: List[str] = []


class TemplateRender(BaseModel):
    ok: bool                              # no errors: Create would be accepted
    params: Dict[str, Any] = {}           # the parameters after defaults / type conversion
    group: Optional[Dict[str, Any]] = None
    cluster: Optional[Dict[str, Any]] = None
    yaml: str = ""                        # group + cluster, editable in the wizard
    guide: str = ""
    hcl: str = ""                         # OpenTofu equivalent
    errors: List[str] = []                # invalid params / spec, conflicts on this host
    warnings: List[str] = []
    estimate: TemplateEstimate = TemplateEstimate()


class TemplateCreated(BaseModel):
    group_id: int
    group_name: str
    task_id: int
    cluster_task_id: Optional[int] = None   # waits for the group, then creates the cluster


class TemplateSave(BaseModel):
    """Save an existing group as a user template"""
    group_id: int
    id: str = Field(..., pattern=TEMPLATE_ID)
    title: str = Field(..., min_length=1, max_length=120)
    summary: str = Field("", max_length=500)
    tags: List[str] = []
    guide: Optional[str] = None           # default: the group's own guide (created from a template), else a stub
