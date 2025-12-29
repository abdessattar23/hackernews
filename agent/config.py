import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    ai_base_url: str
    ai_api_key: str
    blog_model: str
    darija_model: str
    prompts_model: str
    image_model: str

    s3_bucket: str
    s3_prefix: str

    db_path: str
    output_dir: str

    max_items: int


def load_config() -> Config:
    ai_base_url = os.environ.get("AI_BASE_URL", "https://ai.hackclub.com/proxy/v1/chat/completions")
    ai_api_key = os.environ.get("AI_API_KEY") or os.environ.get("HACKCLUB_API_KEY") or ""

    blog_model = os.environ.get("BLOG_MODEL", "qwen/qwen3-32b")
    darija_model = os.environ.get("DARIJA_MODEL", blog_model)
    prompts_model = os.environ.get("PROMPTS_MODEL", darija_model)
    image_model = os.environ.get("IMAGE_MODEL", "google/gemini-2.5-flash-image-preview")

    s3_bucket = os.environ.get("S3_BUCKET", "")
    s3_prefix = os.environ.get("S3_PREFIX", "hn-generated").strip("/")

    db_path = os.environ.get("DB_PATH", "agent_state.sqlite")
    output_dir = os.environ.get("OUTPUT_DIR", "agent_output")

    max_items_raw = os.environ.get("MAX_ITEMS", "5")
    try:
        max_items = int(max_items_raw)
    except ValueError:
        max_items = 5

    return Config(
        ai_base_url=ai_base_url,
        ai_api_key=ai_api_key,
        blog_model=blog_model,
        darija_model=darija_model,
        prompts_model=prompts_model,
        image_model=image_model,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        db_path=db_path,
        output_dir=output_dir,
        max_items=max_items,
    )
