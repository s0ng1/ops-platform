"""IP 段自动发现：异步扫描任务 + 一键入库。"""
import ipaddress
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..collectors import scanner, snmp
from ..core.database import SessionLocal, get_db
from ..core.jobrunner import runner
from ..models import Credential, Device, DiscoveryJob
from .deps import get_current_user, require_operator
from .schemas import DiscoveryJobOut, ImportIn, ScanIn

router = APIRouter(prefix="/api/discovery", tags=["自动发现"])
log = logging.getLogger(__name__)

# 单次批量入库上限：防灌入脏数据/库膨胀
MAX_IMPORT = 5000


def _is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _job_out(job: DiscoveryJob) -> DiscoveryJobOut:
    try:
        results = json.loads(job.result_json)
    except json.JSONDecodeError:
        results = []
    return DiscoveryJobOut(
        id=job.id,
        ranges=job.ranges,
        status=job.status,
        total=job.total,
        done=job.done,
        results=results,
        error=job.error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


async def _snmp_probe(ip: str, payload: dict) -> dict:
    """对存活主机探 SNMP 系统信息，失败返回空 dict。"""
    try:
        return await snmp.get_system_info(ip, payload)
    except Exception:  # noqa: BLE001
        return {}


async def _run_scan(job_id: int, ips: list[str], credential_id: int | None) -> None:
    """后台扫描协程：ping 扫段 → 可选 SNMP 识别 → 结果写回任务记录。"""
    db = SessionLocal()
    try:
        job = db.get(DiscoveryJob, job_id)
        if job is None:
            return
        job.total = len(ips)
        db.commit()

        def on_progress(done: int, total: int) -> None:
            # 进度回写频率限制：每 50 个一刷，避免刷屏式写库
            if done % 50 == 0 or done == total:
                j = db.get(DiscoveryJob, job_id)
                if j:
                    j.done = done
                    db.commit()

        ping_results = await scanner.ping_sweep(ips, on_progress)

        # 存活 IP 回写 IPAM 台账（source=ping），新终端产告警指标点；
        # 最小侵入挂钩：任何失败静默，不阻塞扫描主流程
        try:
            from ..alerting import engine as alert_engine
            from ..collectors import ipam

            alive = [ip for ip, (online, _) in ping_results.items() if online]
            ipam_points = await ipam.upsert_scan_results(alive)
            if ipam_points:
                await alert_engine.evaluate_points(ipam_points)
        except Exception:  # noqa: BLE001
            log.exception("IPAM 台账回写失败 job_id=%s", job_id)

        snmp_payload = None
        if credential_id:
            cred = db.get(Credential, credential_id)
            if cred and cred.kind in ("snmp_v2c", "snmp_v3"):
                snmp_payload = cred.get_payload()
                snmp_payload["kind"] = cred.kind

        existing = {d.ip for d in db.query(Device).all()}
        results = []
        for ip in ips:
            online, latency = ping_results.get(ip, (False, None))
            item = {
                "ip": ip,
                "online": online,
                "latency_ms": latency,
                "already_added": ip in existing,
                "sys_name": "",
                "sys_descr": "",
            }
            if online and snmp_payload:
                info = await _snmp_probe(ip, snmp_payload)
                item["sys_name"] = info.get("sys_name", "")
                item["sys_descr"] = info.get("sys_descr", "")[:200]
            results.append(item)

        job = db.get(DiscoveryJob, job_id)
        job.done = len(ips)
        job.status = "done"
        job.result_json = json.dumps(results, ensure_ascii=False)
        job.finished_at = datetime.now()
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("扫描任务失败 job_id=%s", job_id)
        db.rollback()
        job = db.get(DiscoveryJob, job_id)
        if job:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = datetime.now()
            db.commit()
    finally:
        db.close()


@router.post("/scan", status_code=202)
async def start_scan(
    body: ScanIn,
    db: Session = Depends(get_db),
    _: object = Depends(require_operator),
):
    ips, errors = scanner.parse_ranges(body.ranges)
    if not ips:
        raise HTTPException(
            status_code=400,
            detail="没有可扫描的 IP；" + ("；".join(errors) if errors else "输入为空"),
        )
    if body.credential_id is not None and db.get(Credential, body.credential_id) is None:
        raise HTTPException(status_code=400, detail="凭据不存在")
    job = DiscoveryJob(ranges=body.ranges)
    db.add(job)
    db.commit()
    db.refresh(job)
    runner.submit(_run_scan(job.id, ips, body.credential_id))
    return {"job_id": job.id, "total": len(ips), "parse_errors": errors}


@router.get("/jobs", response_model=list[DiscoveryJobOut])
def list_jobs(
    db: Session = Depends(get_db), _: object = Depends(get_current_user)
):
    jobs = db.query(DiscoveryJob).order_by(DiscoveryJob.id.desc()).limit(20).all()
    return [_job_out(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=DiscoveryJobOut)
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    job = db.get(DiscoveryJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _job_out(job)


@router.post("/import", status_code=201)
def import_devices(
    body: ImportIn,
    db: Session = Depends(get_db),
    _: object = Depends(require_operator),
):
    """把扫描结果中的 IP 批量入库，同 IP 同类型已存在的跳过。"""
    if body.credential_id is not None and db.get(Credential, body.credential_id) is None:
        raise HTTPException(status_code=400, detail="凭据不存在")
    if len(body.ips) > MAX_IMPORT:
        raise HTTPException(status_code=400, detail=f"单次最多导入 {MAX_IMPORT} 台")
    invalid = [ip for ip in body.ips if not _is_valid_ip(ip)]
    if invalid:
        raise HTTPException(
            status_code=400, detail="包含非法 IP：" + ", ".join(invalid[:10])
        )
    existing = {(d.ip, d.type) for d in db.query(Device).all()}
    created = 0
    for ip in dict.fromkeys(body.ips):
        if (ip, body.type) in existing:
            continue
        db.add(
            Device(
                name=ip,
                ip=ip,
                type=body.type,
                group_name=body.group_name,
                location=body.location,
                credential_id=body.credential_id,
            )
        )
        created += 1
    db.commit()
    return {"created": created, "skipped": len(set(body.ips)) - created}
