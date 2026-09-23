"""Admin → System: the full data reset (backend/admin/reset.py has the rules and the reasoning).

Three steps, each a plain form: the System page (type DELETE ALL DATA + your password) -> the final
confirmation page (exact counts, one "Yes" button carrying a one-time confirmation) -> the result, shown back
on the System page from the audit log, so it is still true after a refresh or a server restart.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from backend import photo_storage
from backend.admin import reset as reset_svc
from backend.admin.queries import iso_local as event_clock_time
from backend.security.deps import require_admin
from backend.security.sessions import Principal
from backend.web import redirect, render

router = APIRouter(prefix="/admin/system", dependencies=[Depends(require_admin)])


def _store(request: Request):
    return getattr(request.app.state, "photo_store", None) or photo_storage.current_store()


def _photo_sentence(result) -> str:
    if result.failed:
        return f"{result.failed} photo(s) could not be removed from photo storage."
    return result.error or "The photo storage could not be cleaned up."


@router.get("")
def system_page(request: Request, principal: Principal = Depends(require_admin)):
    with request.app.state.engine.connect() as conn:
        now = reset_svc.labelled(reset_svc.counts(conn))
        last = reset_svc.last_reset(conn)
    if last is not None:
        stamp = event_clock_time(last["occurred_at"], request.app.state.settings.event_utc_offset_minutes)
        last["when"] = stamp.replace("T", " ") if stamp else ""
    return render(request, "admin_system.html", principal=principal, counts=now, last=last,
                  phrase=reset_svc.CONFIRM_PHRASE, storage=_store(request).describe())


@router.post("/reset")
def reset_request(request: Request, phrase: str = Form(""), password: str = Form(""),
                  principal: Principal = Depends(require_admin)):
    engine = request.app.state.engine
    try:
        token = reset_svc.request_confirmation(engine, principal, phrase=phrase, password=password)
    except reset_svc.ResetError as exc:
        return redirect("/admin/system", error=exc.message)
    with engine.connect() as conn:
        now = reset_svc.labelled(reset_svc.counts(conn))
    return render(request, "admin_system_confirm.html", principal=principal, counts=now, confirmation=token,
                  storage=_store(request).describe(), ttl=reset_svc.CONFIRM_TTL_MINUTES)


@router.post("/reset/execute")
def reset_execute(request: Request, confirmation: str = Form(""), principal: Principal = Depends(require_admin)):
    try:
        outcome = reset_svc.execute_reset(request.app.state.engine, principal, confirmation=confirmation,
                                          store=_store(request), settings=request.app.state.settings)
    except reset_svc.ResetError as exc:
        return redirect("/admin/system", error=exc.message)
    students = outcome.deleted.get("students", 0)
    if outcome.complete:
        return redirect("/admin/system", msg=f"All data was deleted ({students} student(s)). "
                                             f"{outcome.photos.deleted} photo(s) were removed from photo storage.")
    return redirect("/admin/system", error=f"All data was deleted ({students} student(s)), but the photo clean-up "
                                           f"did not finish: {_photo_sentence(outcome.photos)} Please use Retry photo clean-up.")


@router.post("/reset/photo-cleanup")
def photo_cleanup(request: Request, principal: Principal = Depends(require_admin)):
    try:
        result = reset_svc.retry_photo_cleanup(request.app.state.engine, principal, store=_store(request))
    except reset_svc.ResetError as exc:
        return redirect("/admin/system", error=exc.message)
    if result.complete:
        return redirect("/admin/system", msg=f"Photo clean-up finished. {result.deleted} photo(s) were removed.")
    return redirect("/admin/system", error=f"The photo clean-up did not finish: {_photo_sentence(result)} "
                                           f"Please try again later.")
