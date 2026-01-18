from pydantic import BaseModel

class EmailVerificationRequiredResponse(BaseModel):
    message: str
    email_not_verified: bool