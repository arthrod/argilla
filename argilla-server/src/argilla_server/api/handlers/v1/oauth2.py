#  Copyright 2021-present, the Recognai S.L. team.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Path
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from argilla_server import telemetry
from argilla_server.api.schemas.v1.oauth2 import Provider, Providers, Token
from argilla_server.api.schemas.v1.users import UserCreate
from argilla_server.contexts import accounts
from argilla_server.database import get_async_db
from argilla_server.enums import UserRole
from argilla_server.errors.future import AuthenticationError, NotFoundError
from argilla_server.models import User
from argilla_server.pydantic_v1 import Field, ValidationError
from argilla_server.security.authentication.jwt import JWT
from argilla_server.security.authentication.oauth2 import OAuth2ClientProvider
from argilla_server.security.authentication.userinfo import UserInfo
from argilla_server.security.settings import settings

router = APIRouter(prefix="/oauth2", tags=["Authentication"])


class UserOAuthCreate(UserCreate):
    """This schema is used to validate the creation of a new user by using the oauth userinfo"""

    username: str = Field(min_length=1)
    role: Optional[UserRole]
    password: Optional[str] = None


def get_provider_by_name_or_raise(provider: str = Path()) -> OAuth2ClientProvider:
    if not settings.oauth.enabled:
        raise NotFoundError(message="OAuth2 is not enabled")

    if provider in settings.oauth.providers:
        return settings.oauth.providers[provider]

    raise NotFoundError(message=f"OAuth Provider '{provider}' not found")


@router.get("/providers", response_model=Providers)
def list_providers() -> Providers:
    if not settings.oauth.enabled:
        return Providers(items=[])

    return Providers(items=[Provider(name=provider_name) for provider_name in settings.oauth.providers])


@router.get("/providers/{provider}/authentication")
def get_authentication(
    request: Request,
    provider: OAuth2ClientProvider = Depends(get_provider_by_name_or_raise),
) -> RedirectResponse:
    return provider.authorization_redirect(request)


@router.get("/providers/{provider}/access-token", response_model=Token)
async def get_access_token(
    request: Request,
    provider: OAuth2ClientProvider = Depends(get_provider_by_name_or_raise),
    db: AsyncSession = Depends(get_async_db),
) -> Token:
    try:
        user_info = UserInfo(await provider.get_user_data(request)).use_claims(provider.claims)
        user = await User.get_by(db, username=user_info.username)
        if user is None:
            try:
                user_create = UserOAuthCreate(
                    username=user_info.username,
                    first_name=user_info.first_name,
                    role=user_info.role,
                )
            except ValidationError as ex:
                raise AuthenticationError("Could not authenticate user") from ex

            user = await accounts.create_user_with_random_password(
                db,
                **user_create.dict(exclude_unset=True),
                workspaces=[workspace.name for workspace in settings.oauth.allowed_workspaces],
            )
            telemetry.track_user_created(user, is_oauth=True)

        elif user.role != user_info.role:
            raise AuthenticationError("Could not authenticate user")

        return Token(access_token=JWT.create(user_info))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # TODO: Create exception handler for AuthenticationError
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
