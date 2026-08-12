"""
動作確認用のサンプル: APIの「契約」(リクエスト/レスポンスの形) を表すエンドポイント。
このファイルはAIレビューBotの契約変更検知ルールをテストするために使われます。
"""
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class UserProfile(BaseModel):
    name: str
    email: str


@app.get("/api/user", response_model=UserProfile)
def get_user() -> UserProfile:
    return UserProfile(name="山田太郎", email="yamada@example.com")
