# SPDX-License-Identifier: Apache-2.0
"""
Multi-tenant security decorator for enforcing tenant/user data access control.

The require_tenant_access decorator verifies that the current request identity
matches the target tenant and user before allowing function execution.
Admin users (role='admin') bypass tenant checks.
"""

from functools import wraps
from typing import Any, Callable, TypeVar

from opencortex.http.request_context import (
    get_effective_identity,
    is_admin,
)

F = TypeVar("F", bound=Callable[..., Any])


def require_tenant_access(tenant_id: str, user_id: str) -> Callable[[F], F]:
    """
    Decorator that enforces tenant/user access control.

    Reads current identity from JWT context via get_effective_identity().
    Allows access if:
    - Current tenant_id and user_id match the protected resource, OR
    - Current user is admin (user_id='admin')

    Raises PermissionError if access is denied.

    Args:
        tenant_id: The tenant ID of the resource being protected
        user_id: The user ID of the resource being protected

    Returns:
        Decorated function that checks access before execution

    Raises:
        PermissionError: If current identity does not match and user is not admin
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            current_tenant, current_user = get_effective_identity()

            if is_admin():
                return func(*args, **kwargs)

            if current_tenant != tenant_id or current_user != user_id:
                raise PermissionError(
                    f"Access denied: user {current_user}@{current_tenant} "
                    f"cannot access {user_id}@{tenant_id}"
                )

            return func(*args, **kwargs)

        return wrapper  # type: ignore

    return decorator
