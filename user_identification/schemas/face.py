from pydantic import BaseModel, Field


class FaceSearchItem(BaseModel):
    entity_id: str
    face_id: str
    score: float
    confidence: float
    db_name: str = ""
    extra_data: str = ""
    qualitie_score: float = 0.0


class FaceSearchResponse(BaseModel):
    results: list[FaceSearchItem] = Field(default_factory=list)
    request_id: str = ""


class FaceVerifyResponse(BaseModel):
    user_id: str
    entity_id: str = ""
    face_id: str = ""
    score: float = 0.0
    confidence: float = 0.0
    match: bool
    threshold: float = 0.5
    db_name: str = ""
    request_id: str = ""


class FaceLocation(BaseModel):
    x: int
    y: int
    width: int
    height: int


class LandmarkPoint(BaseModel):
    x: float
    y: float


class FaceDetectItem(BaseModel):
    user_id: str
    name: str = ""
    score: float = 0.0
    matched: bool
    location: FaceLocation | None = None
    mouth: list[LandmarkPoint] = Field(default_factory=list)


class FaceDetectResponse(BaseModel):
    faces: list[FaceDetectItem] = Field(default_factory=list)
    total_faces: int = 0
    matched_count: int = 0
    unknown_count: int = 0
    request_id: str = ""


class FaceAnalyzeItem(BaseModel):
    bbox: list[float]
    det_score: float
    kps: list[list[float]] | None = None
    pose: list[float] | None = None


class FaceAnalyzeResponse(BaseModel):
    faces: list[FaceAnalyzeItem] = Field(default_factory=list)
    request_id: str = ""