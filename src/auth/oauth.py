"""GitHub OAuth configuration using Authlib."""

from authlib.integrations.starlette_client import OAuth
from starlette.config import Config

from src.config import get_settings

settings = get_settings()

# Authlib needs a Starlette Config object for session handling
starlette_config = Config(environ={
    "GITHUB_CLIENT_ID": settings.github_client_id,
    "GITHUB_CLIENT_SECRET": settings.github_client_secret,
})

oauth = OAuth(starlette_config)

oauth.register(
    name="github",
    access_token_url="https://github.com/login/oauth/access_token",
    access_token_params=None,
    authorize_url="https://github.com/login/oauth/authorize",
    authorize_params=None,
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
)
