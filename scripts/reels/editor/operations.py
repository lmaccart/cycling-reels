"""The editing operation inventory, exposed as real MCP JSON Schema."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class OperationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RevisionTokens(OperationModel):
    document: str
    library: str
    renderer: str


class SetField(OperationModel):
    """Use a stable node ID and a pointer RELATIVE to that node, e.g. /volume.
    Use id=document for /hook/text; id=project for /render/fps. Never /segments/0/volume."""
    op: Literal["set_field", "set_keyframes"]
    id: str
    path: str = Field(pattern=r"^/")
    value: Any


class InsertSegment(OperationModel):
    op: Literal["insert_segment"]
    value: dict[str, Any] = Field(description="Existing reel segment schema: asset, source in/out seconds or moment, framing, audio, effects.")
    beforeId: str | None = None


class RemoveSegments(OperationModel):
    op: Literal["remove_segments"]
    ids: list[str] = Field(min_length=1)


class DuplicateSegments(OperationModel):
    """Copy primary segments (linked cutaways, text, sfx… come with them, all with new IDs).

    Sequential plans: the copies go directly after the last original, in plan order, and later
    clips ripple right. Positioned plans: the copies sit after the last original's end on the same
    tracks, shifted by the span of the selection; nothing else moves, and an occupied range is an error."""
    op: Literal["duplicate_segments"]
    ids: list[str] = Field(min_length=1)


class RippleRemove(OperationModel):
    """Remove primary segments and close the gap, in positioned plans too (remove_segments leaves a
    gap there). Positioned plans shift every later clip on every track left by the removed length;
    a clip that straddles the removed range is an error."""
    op: Literal["ripple_remove"]
    ids: list[str] = Field(min_length=1)


class PasteSegments(OperationModel):
    """Insert copies of segment dicts (as returned by get_project; timeline/_editor/ID metadata are
    stripped and reissued). Sequential plans insert at the nearest cut at or before atFrame.
    Positioned plans place them at atFrame on track (default: first video track) with the
    overwrite rules of move_segments; relative offsets in the values' own timeline are kept."""
    op: Literal["paste_segments"]
    values: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    atFrame: int = Field(ge=0, description="Global reel frame.")
    track: str | None = Field(default=None, pattern=r"^V[1-9][0-9]*$")


class SetEnabled(OperationModel):
    """Disable or enable primary segments. A disabled segment keeps its place and full length in the
    editor but renders no video and no audio; sequential plans close up around it. Stored as
    "enabled": false on the segment (the key is absent when enabled)."""
    op: Literal["set_enabled"]
    ids: list[str] = Field(min_length=1)
    enabled: StrictBool


class ReorderSegments(OperationModel):
    op: Literal["reorder_segments"]
    ids: list[str] = Field(min_length=1)
    beforeId: str | None = None


class FootageInsert(BaseModel):
    """Renderer segment settings, with an explicit asset and no editor metadata."""
    model_config = ConfigDict(extra="allow", allow_inf_nan=False, json_schema_extra={
        "not": {"anyOf": [{"required": ["timeline"]}, {"required": ["_editor"]}]},
    })
    asset: str = Field(min_length=1, pattern=r"^[^\x00]+$")

    @model_validator(mode="after")
    def renderer_fields_only(self):
        if any(key in (self.model_extra or {}) for key in ("timeline", "_editor")):
            raise ValueError("insert cannot supply timeline/_editor metadata")
        return self


class FootagePlacement(OperationModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, json_schema_extra={
        "oneOf": [{"required": ["id"], "properties": {"id": {"type": "string"}},
                   "not": {"required": ["insert"]}},
                  {"required": ["insert"], "properties": {"insert": {"type": "object"}},
                   "not": {"required": ["id"]}}],
    })
    track: str = Field(pattern=r"^V[1-9][0-9]*$")
    atFrame: int = Field(ge=0)
    id: str | None = Field(default=None, min_length=1, pattern=r"^[^\x00]+$")
    insert: FootageInsert | None = None

    @model_validator(mode="after")
    def source(self):
        if (("id" in self.model_fields_set) == ("insert" in self.model_fields_set)
                or (self.id is None) == (self.insert is None)):
            raise ValueError("provide exactly one of id or insert")
        return self


class MoveSegments(OperationModel):
    """One non-ripple, destination-track-only footage overwrite."""
    op: Literal["move_segments"]
    editId: str = Field(json_schema_extra={"format": "uuid"})
    mode: Literal["overwrite"]
    placements: list[FootagePlacement] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_edit_id(self):
        from ..timeline import edit_id
        self.editId = edit_id(self.editId)
        return self


class TrimSegment(OperationModel):
    op: Literal["trim_segment", "trim_cutaway"]
    id: str
    sourceIn: float = Field(ge=0, description="Seconds in the registered original, not timeline seconds.")
    sourceOut: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.sourceOut <= self.sourceIn:
            raise ValueError("sourceOut must be later than sourceIn")
        return self


class SplitSegment(OperationModel):
    op: Literal["split_segment"]
    id: str
    atFrame: int = Field(ge=1, description="Global half-open reel frame inside this segment.")


class InsertOverlay(OperationModel):
    op: Literal["insert_overlay"]
    parentId: str = Field(description="Owning segment stable ID, or document for reel-global overlays.")
    collection: str = Field(description="Existing collection such as cutaways, text, punch, blur, exhibits, sfx, stats or musicDrops; not segments.")
    value: dict[str, Any]


class RemoveOverlay(OperationModel):
    op: Literal["remove_overlay"]
    id: str


class MoveOverlay(OperationModel):
    op: Literal["move_overlay"]
    id: str
    at: float = Field(ge=0, description="Schema-local reel seconds, relative to its owning segment or the reel.")


class ResizeOverlay(OperationModel):
    op: Literal["resize_overlay"]
    id: str
    at: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, alias="for", ge=0)

    @model_validator(mode="after")
    def has_change(self):
        if self.at is None and self.duration is None:
            raise ValueError("provide at and/or for")
        return self


class WordCorrection(OperationModel):
    index: int = Field(ge=0)
    word: str


class CorrectWords(OperationModel):
    op: Literal["correct_words"]
    assetId: str
    corrections: list[WordCorrection] = Field(min_length=1)


class AddTrack(OperationModel):
    """Add an organisational V/A track (next free number). Never changes the render."""
    op: Literal["add_track"]
    kind: Literal["video", "audio"]


class TrackRef(OperationModel):
    op: Literal["remove_track", "set_track_locked", "rename_track"]
    id: str = Field(pattern=r"^[VA][0-9]+$")
    name: str | None = Field(default=None, min_length=1, max_length=40)
    locked: bool | None = None

    @model_validator(mode="after")
    def has_fields(self):
        if self.op == "rename_track" and self.name is None:
            raise ValueError("rename_track needs name")
        if self.op == "set_track_locked" and self.locked is None:
            raise ValueError("set_track_locked needs locked")
        return self


class SetNodeTrack(OperationModel):
    """Assign an item to a track of its own kind (video items to V, audio items to A)."""
    op: Literal["set_node_track"]
    id: str
    track: str = Field(pattern=r"^[VA][0-9]+$")


EditingOperation = Annotated[
    Union[SetField, InsertSegment, RemoveSegments, ReorderSegments, MoveSegments, TrimSegment,
          SplitSegment, InsertOverlay, RemoveOverlay, MoveOverlay, ResizeOverlay, CorrectWords,
          AddTrack, TrackRef, SetNodeTrack, DuplicateSegments, RippleRemove, PasteSegments,
          SetEnabled],
    Field(discriminator="op"),
]


class OperationBatch(OperationModel):
    """One atomic, undoable edit. Read get_project for tokens and stable IDs first."""
    expected: RevisionTokens
    reason: str = Field(min_length=1, max_length=1000)
    operations: list[EditingOperation] = Field(min_length=1, max_length=500)

    def wire(self):
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def operation_schema():
    return OperationBatch.model_json_schema()
