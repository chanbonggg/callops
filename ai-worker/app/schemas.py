"""Input and output contracts for the standalone transcript analyzer."""

from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


SegmentID = Annotated[int, Field(gt=0)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Title = Annotated[Text, Field(max_length=160)]
Speaker = Literal["A", "B"]
AnalysisStatus = Literal["confirmed", "proposed", "cancelled", "uncertain"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Segment(StrictModel):
    segment_id: SegmentID
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: Speaker | None
    text: Text


class Transcript(StrictModel):
    segments: list[Segment] = Field(min_length=1)
    timezone: str | None = None
    # Only a trusted source may supply the call time. Missing and unverified times stay null.
    call_started_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_ranges_and_clock(self):
        ids = set()
        previous_start = 0
        for segment in self.segments:
            if segment.segment_id in ids or not previous_start <= segment.start <= segment.end:
                raise ValueError("Invalid transcript segment order, bounds or IDs")
            previous_start = segment.start
            ids.add(segment.segment_id)
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError("Invalid call time zone") from error
        return self


class Evidence(StrictModel):
    segment_ids: list[SegmentID] = Field(min_length=1, max_length=10)


class Summary(Evidence):
    text: Annotated[Text, Field(max_length=4000)]


class ExtractedEvent(Evidence):
    title: Title
    status: AnalysisStatus
    date_text: Text | None
    time_text: Text | None


class ExtractedTask(Evidence):
    title: Title
    status: AnalysisStatus
    deadline_text: Text | None
    assignee_segment_id: SegmentID | None


class ContractMention(Evidence):
    text: Text


class ReviewItem(Evidence):
    reason: Text


class Extraction(StrictModel):
    summary: Summary
    events: list[ExtractedEvent]
    tasks: list[ExtractedTask]
    contract_mentions: list[ContractMention]
    needs_review: list[ReviewItem]

    def items(self):
        return (self.events, self.tasks, self.contract_mentions, self.needs_review)


class Event(ExtractedEvent):
    start_at: datetime | None
    end_at: datetime | None


class Task(ExtractedTask):
    assignee: Speaker | None
    due_at: datetime | None


class CallAnalysis(Extraction):
    schema_version: Literal["2.0"] = "2.0"
    events: list[Event]
    tasks: list[Task]
