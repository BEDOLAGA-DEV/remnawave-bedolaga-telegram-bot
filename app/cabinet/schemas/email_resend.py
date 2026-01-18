from pydantic import BaseModel, EmailStr, Field

class EmailResendRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address")
    password: str = Field(..., min_length=8, max_length=128, description="Password")
