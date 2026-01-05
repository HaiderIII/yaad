"""Authentication-related Pydantic models."""

from pydantic import BaseModel


class GitHubUser(BaseModel):
    """GitHub user data from OAuth."""

    id: int
    login: str
    email: str | None = None
    avatar_url: str | None = None
    name: str | None = None
