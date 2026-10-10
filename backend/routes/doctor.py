"""Doctor page and diagnostics bundle (v0.10.0) — see backend/doctor.py."""
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(prefix="/api/doctor")


@router.get("")
async def get_doctor():
    from backend.doctor import run_checks
    return await run_checks()


@router.get("/diagnostics")
async def get_diagnostics():
    from backend.doctor import diagnostics_bundle
    name, data = await diagnostics_bundle()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
