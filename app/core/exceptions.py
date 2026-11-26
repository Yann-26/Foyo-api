from fastapi import HTTPException, status


def not_found(what: str = "Ressource") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} introuvable")


def forbidden(msg: str = "Action non autorisée pour votre rôle") -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, msg)


def bad_request(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, msg)


def conflict(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, msg)
