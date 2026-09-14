from pydantic import BaseModel


class UserFaceResponse(BaseModel):
    user_id: str
    name: str = ""
    face_image: str | None = None


class UserRegisterResponse(BaseModel):
    user_id: str
    name: str
    role: str = ""
    description: str = ""
    face_id: str | None = None
    message: str = "User registered successfully"


class UserDeleteResponse(BaseModel):
    user_id: str
    deleted: bool
    message: str = "User deleted successfully"


class UserInfoResponse(BaseModel):
    user_id: str
    name: str = ""
    role: str = ""
    description: str = ""
    created_at: str = ""
    exists: bool = True


class UserItem(BaseModel):
    user_id: str
    name: str
    role: str = ""
    description: str = ""
    created_at: str = ""


class UserListResponse(BaseModel):
    total: int
    users: list[UserItem]