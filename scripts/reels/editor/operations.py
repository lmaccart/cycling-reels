"""The editing operation inventory, exposed as real MCP JSON Schema."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class ReorderSegments(OperationModel):
    op: Literal["reorder_segments"]
    ids: list[str] = Field(min_length=1)
    beforeId: str | None = None


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


EditingOperation = Annotated[
    Union[SetField, InsertSegment, RemoveSegments, ReorderSegments, TrimSegment,
          SplitSegment, InsertOverlay, RemoveOverlay, MoveOverlay, ResizeOverlay, CorrectWords],
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
