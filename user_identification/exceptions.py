class AppBaseError(Exception):
    status_code: int = 500
    detail: str = "Internal server error"
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, detail: str | None = None):
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class ThirdPartyAPIError(AppBaseError):
    status_code = 502
    error_code = "THIRD_PARTY_API_ERROR"


class FaceComparisonError(ThirdPartyAPIError):
    status_code = 422
    detail = "Face comparison failed"
    error_code = "FACE_COMPARISON_FAILED"


class VoiceTaskFailedError(ThirdPartyAPIError):
    status_code = 422
    error_code = "VOICE_TASK_FAILED"

    def __init__(self, detail: str | None = None, api_code: int | None = None):
        super().__init__(detail=detail)
        self.api_code = api_code


class InvalidAudioError(AppBaseError):
    status_code = 400
    detail = "Invalid audio format"
    error_code = "INVALID_AUDIO"


class FaceEnrollmentError(ThirdPartyAPIError):
    status_code = 422
    detail = "Face enrollment failed"
    error_code = "FACE_ENROLLMENT_FAILED"


class FaceDatabaseError(ThirdPartyAPIError):
    status_code = 422
    detail = "Face database operation failed"
    error_code = "FACE_DATABASE_ERROR"


class FaceStorageError(AppBaseError):
    status_code = 500
    detail = "Face storage operation failed"
    error_code = "FACE_STORAGE_ERROR"