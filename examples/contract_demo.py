from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class UserProfile(BaseModel):
    name: str
    contact: str


@app.get("/api/user", response_model=UserProfile)
def get_user() -> UserProfile:
    return UserProfile(name="山田太郎", contact="yamada@example.com")
