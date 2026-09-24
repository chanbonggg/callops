import json
from pathlib import Path

from ollama import chat

from app.schemas import CallAnalysis


MODEL_NAME = "qwen3:8b"
PROMPT_VERSION = "analysis-v1"
NUM_CTX = 8192

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = BASE_DIR / "prompts" / "analysis_v1.txt"


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def analyze_transcript(transcript: str) -> CallAnalysis:
    """
    전사본을 Qwen3 8B로 분석하고
    CallAnalysis 스키마로 검증하여 반환한다.
    """

    if not transcript.strip():
        raise ValueError("Transcript is empty.")

    system_prompt = load_system_prompt()
    schema = CallAnalysis.model_json_schema()

    user_prompt = f"""
아래 JSON Schema에 맞춰 전사본을 분석하라.

JSON Schema:
{json.dumps(schema, ensure_ascii=False)}

<transcript>
{transcript}
</transcript>
""".strip()

    response = chat(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        format=schema,
        options={
            "temperature": 0,
            "num_ctx": NUM_CTX,
        },
    )

    return CallAnalysis.model_validate_json(
        response.message.content
    )


if __name__ == "__main__":
    sample_transcript = """
[1] 김대리님, 내일 오후 3시에 미팅하죠.
[2] 아, 3시는 어렵고 오후 4시로 변경하겠습니다.
[3] 네, 내일 오후 4시로 확정하겠습니다.
[4] 자료는 제가 오늘 저녁까지 보내겠습니다.
[5] 계약 금액은 아직 확정하지 않았습니다.
""".strip()

    result = analyze_transcript(sample_transcript)

    print(result.model_dump_json(indent=2))
