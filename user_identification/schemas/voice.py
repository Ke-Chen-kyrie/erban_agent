from pydantic import BaseModel, Field


class VoiceSearchItem(BaseModel):
    score: float
    feature_info: str = ""
    user_id: str = ""
    name: str = ""


class VoiceSearchResponse(BaseModel):
    score_list: list[dict] = Field(default_factory=list)
    results: list[VoiceSearchItem] = Field(default_factory=list)