from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------
# 공통
# ---------------------------------------------------------

AnalysisStatus = Literal[
    "confirmed",
    "proposed",
    "cancelled",
    "uncertain",
]


class Evidence(BaseModel):
    """
    분석 결과가 전사본의 어느 segment를 근거로 했는지 기록한다.
    """

    segment_ids: list[int] = Field(
        min_length=1,
        description="해당 판단의 근거가 된 전사 segment ID 목록",
    )


# ---------------------------------------------------------
# 일정
# ---------------------------------------------------------

class Event(BaseModel):
    """
    통화에서 발견된 일정/약속/미팅 등의 후보.
    """

    title: str = Field(
        min_length=1,
        description="일정 제목. 예: 고객사 방문, 프로젝트 미팅",
    )

    date_text: str | None = Field(
        default=None,
        description=(
            "전사에서 직접 언급된 날짜 표현. "
            "예: '내일', '9월 25일', '다음 주 화요일'. "
            "날짜가 언급되지 않은 경우에만 null."
        ),
    )

    time_text: str | None = Field(
        default=None,
        description=(
            "전사에서 직접 언급된 시간 표현. "
            "예: '오후 4시', '아침 10시'. "
            "정정된 경우 최종 시간을 기록한다. "
            "시간이 언급되지 않은 경우에만 null."
        ),
    )

    status: AnalysisStatus = Field(
        description=(
            "confirmed: 확정, "
            "proposed: 제안만 됨, "
            "cancelled: 취소됨, "
            "uncertain: 판단 불명확"
        ),
    )

    evidence: Evidence


# ---------------------------------------------------------
# 할 일
# ---------------------------------------------------------

class Task(BaseModel):
    """
    통화에서 누군가 수행하기로 한 할 일.
    """

    title: str = Field(
        min_length=1,
        description="수행해야 하는 일. 예: 견적서 전달",
    )

    deadline_text: str | None = Field(
        default=None,
        description=(
            "전사에서 직접 언급된 마감 표현. "
            "예: '오늘 저녁까지', '금요일까지'. "
            "기한이 언급되지 않은 경우에만 null."
        ),
    )

    status: AnalysisStatus

    evidence: Evidence


# ---------------------------------------------------------
# 계약 관련 언급
# ---------------------------------------------------------

class ContractMention(BaseModel):
    """
    계약, 가격, 발주, 결제 등 법적/금전적으로 중요한 언급.

    실제 계약 체결 여부를 모델이 임의로 판단하지 않는다.
    원문에서 언급된 사실만 기록한다.
    """

    text: str = Field(
        min_length=1,
        description="계약 관련 핵심 내용을 원문의 의미를 보존하여 기록",
    )

    evidence: Evidence


# ---------------------------------------------------------
# 검토 필요 항목
# ---------------------------------------------------------

class ReviewItem(BaseModel):
    """
    모델이 확실하게 판단할 수 없는 내용을 사람이 확인하도록 전달한다.
    """

    reason: str = Field(
        min_length=1,
        description="확인이 필요한 이유",
    )

    evidence: Evidence | None = None


# ---------------------------------------------------------
# 최종 CallOps 분석 결과
# ---------------------------------------------------------

class CallAnalysis(BaseModel):
    """
    한 통화에 대한 CallOps의 최종 구조화 분석 결과.
    """

    schema_version: str = Field(
        default="1.0",
        description="CallOps 분석 결과 스키마 버전",
    )

    summary: str = Field(
        min_length=1,
        description="통화 전체의 간결한 업무 요약",
    )

    events: list[Event] = Field(
        default_factory=list,
    )

    tasks: list[Task] = Field(
        default_factory=list,
    )

    contract_mentions: list[ContractMention] = Field(
        default_factory=list,
    )

    needs_review: list[ReviewItem] = Field(
        default_factory=list,
    )

