"""凭据管理：密文落库，接口永不回显明文。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..models import Credential
from .deps import get_current_user, require_operator
from .schemas import CredentialIn, CredentialOut

router = APIRouter(prefix="/api/credentials", tags=["凭据"])


@router.get("", response_model=list[CredentialOut])
def list_credentials(
    db: Session = Depends(get_db), _: object = Depends(get_current_user)
):
    return db.query(Credential).order_by(Credential.id).all()


@router.post("", response_model=CredentialOut, status_code=201)
def create_credential(
    body: CredentialIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    if db.query(Credential).filter(Credential.name == body.name).first():
        raise HTTPException(status_code=409, detail="同名凭据已存在")
    cred = Credential(name=body.name, kind=body.kind)
    cred.set_payload(body.payload)
    db.add(cred)
    db.commit()
    db.refresh(cred)
    audit.record(user.username, "credential_create", target=cred.name, ip=audit.client_ip(request))
    return cred


@router.put("/{cred_id}", response_model=CredentialOut)
def update_credential(
    cred_id: int,
    body: CredentialIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    cred = db.get(Credential, cred_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    cred.name = body.name
    cred.kind = body.kind
    if body.payload:  # 空 payload 表示不改密钥内容
        cred.set_payload(body.payload)
    db.commit()
    db.refresh(cred)
    audit.record(user.username, "credential_update", target=cred.name, ip=audit.client_ip(request))
    return cred


@router.delete("/{cred_id}")
def delete_credential(
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    cred = db.get(Credential, cred_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    name = cred.name
    db.delete(cred)
    db.commit()
    audit.record(user.username, "credential_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}
