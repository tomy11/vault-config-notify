import os
import json
import logging
import httpx
import hvac
import anthropic

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
VAULT_ADDR          = os.getenv("VAULT_ADDR", "https://vault:8200")
VAULT_TOKEN         = os.getenv("VAULT_TOKEN")
VAULT_SKIP_VERIFY   = os.getenv("VAULT_SKIP_VERIFY", "true").lower() == "true"
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN")
SECRET_TOKEN        = os.getenv("SECRET_TOKEN", "changeme")

# ─── Clients ─────────────────────────────────────────────────────────────────
vault_client = hvac.Client(
    url=VAULT_ADDR,
    token=VAULT_TOKEN,
    verify=not VAULT_SKIP_VERIFY,
)

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

app = FastAPI(title="Vault Approval Server", version="1.0.0")


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def notify_discord(message: str, color: int = 5793266, title: str = "Vault Update") -> None:
    """ส่งแจ้งเตือนไป Discord"""
    payload = {
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
        }]
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        except Exception as e:
            log.warning(f"Discord notify failed: {e}")


async def fetch_github_diff(run_id: str) -> str:
    """ดึง diff จาก GitHub Actions run"""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        # ดึง run info เพื่อหา repo และ commit sha
        run_res = await client.get(
            f"https://api.github.com/repos/{os.getenv('GITHUB_REPO')}/actions/runs/{run_id}",
            headers=headers,
            timeout=10,
        )
        run_data = run_res.json()
        sha = run_data.get("head_sha", "")

        # ดึง diff ของ commit นั้น
        diff_res = await client.get(
            f"https://api.github.com/repos/{os.getenv('GITHUB_REPO')}/commits/{sha}",
            headers={**headers, "Accept": "application/vnd.github.diff"},
            timeout=10,
        )
        return diff_res.text


def analyze_diff_with_ai(diff: str) -> dict:
    """ให้ Claude วิเคราะห์ diff แล้วสร้าง Vault update plan"""
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""วิเคราะห์ config diff นี้ แล้วระบุเฉพาะ field ที่ถูก add หรือ update เท่านั้น
อย่าส่ง field ที่ไม่เปลี่ยนแปลง

ตอบเป็น JSON เท่านั้น ห้ามมีข้อความอื่น:
{{
  "risk": "low | medium | high",
  "summary": "สรุปสั้นๆ ว่าเปลี่ยนอะไร",
  "changes": [
    {{
      "path": "secret/data/app",
      "key": "DB_HOST",
      "action": "add | update | delete",
      "new_value": "ค่าใหม่ (ถ้า action เป็น delete ให้ใส่ null)"
    }}
  ]
}}

diff:
{diff}"""
        }]
    )

    raw = response.content[0].text.strip()

    # ตัด markdown code block ออกถ้ามี
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())


def apply_vault_changes(changes: list) -> tuple[list, list]:
    """
    อัปเดต Vault เฉพาะ field ที่เปลี่ยน (patch) ไม่แตะ key อื่น
    return: (updated_keys, deleted_keys)
    """
    updated, deleted = [], []

    for item in changes:
        path   = item["path"]
        key    = item["key"]
        action = item["action"]

        if action in ("add", "update"):
            # patch เฉพาะ key นั้น ไม่ส่งทั้งก้อน
            vault_client.secrets.kv.v2.patch(
                path=path,
                secret={key: item.get("new_value", "")},
            )
            updated.append(f"`{key}` @ {path}")
            log.info(f"Vault patched: {path} → {key} ({action})")

        elif action == "delete":
            # อ่านค่าทั้งหมด → ลบ key นั้นออก → เขียนกลับ
            current = vault_client.secrets.kv.v2.read_secret_version(
                path=path, raise_on_deleted_version=True,
            )["data"]["data"]
            current.pop(key, None)
            vault_client.secrets.kv.v2.create_or_update_secret(
                path=path,
                secret=current,
            )
            deleted.append(f"`{key}` @ {path}")
            log.info(f"Vault deleted key: {path} → {key}")

    return updated, deleted


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/approve")
async def approve(
    run_id: str = Query(..., description="GitHub Actions run ID"),
    token: str  = Query(..., description="Secret token for verification"),
):
    """รับ Approve จาก Discord → AI วิเคราะห์ diff → update Vault"""

    # ── 1. ตรวจ token ─────────────────────────────────────────────
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    log.info(f"Approve received for run_id={run_id}")
    await notify_discord(
        f"⏳ กำลังวิเคราะห์ diff สำหรับ run `{run_id}`...",
        color=3447003,
        title="Vault Update — Processing",
    )

    # ── 2. ดึง diff จาก GitHub ────────────────────────────────────
    try:
        diff = await fetch_github_diff(run_id)
    except Exception as e:
        log.error(f"Failed to fetch diff: {e}")
        await notify_discord(f"❌ ดึง diff ไม่สำเร็จ: {e}", color=16711680, title="Vault Update — Error")
        raise HTTPException(status_code=500, detail=str(e))

    # ── 3. AI วิเคราะห์ diff ─────────────────────────────────────
    try:
        plan = analyze_diff_with_ai(diff)
    except Exception as e:
        log.error(f"AI analysis failed: {e}")
        await notify_discord(f"❌ AI วิเคราะห์ไม่สำเร็จ: {e}", color=16711680, title="Vault Update — Error")
        raise HTTPException(status_code=500, detail=str(e))

    log.info(f"AI plan: risk={plan['risk']}, changes={len(plan['changes'])}")

    # ── 4. ถ้า risk สูง → หยุด แจ้ง Discord ─────────────────────
    if plan["risk"] == "high":
        await notify_discord(
            f"🚨 **AI ตรวจพบความเสี่ยงสูง — ยกเลิกการอัปเดต**\n\n{plan['summary']}",
            color=16711680,
            title="Vault Update — Blocked",
        )
        return {"status": "blocked", "reason": plan["summary"]}

    # ── 5. Apply changes เข้า Vault ──────────────────────────────
    try:
        updated, deleted = apply_vault_changes(plan["changes"])
    except Exception as e:
        log.error(f"Vault update failed: {e}")
        await notify_discord(f"❌ อัปเดต Vault ไม่สำเร็จ: {e}", color=16711680, title="Vault Update — Error")
        raise HTTPException(status_code=500, detail=str(e))

    # ── 6. แจ้ง Discord ว่าสำเร็จ ────────────────────────────────
    updated_text = "\n".join(updated) if updated else "ไม่มี"
    deleted_text = "\n".join(deleted) if deleted else "ไม่มี"

    await notify_discord(
        f"**สรุป:** {plan['summary']}\n\n"
        f"✅ **Updated/Added:**\n{updated_text}\n\n"
        f"🗑️ **Deleted:**\n{deleted_text}",
        color=65280,
        title="✅ Vault Update — Success",
    )

    return {
        "status": "success",
        "updated": updated,
        "deleted": deleted,
        "summary": plan["summary"],
    }


@app.post("/reject")
async def reject(
    run_id: str = Query(..., description="GitHub Actions run ID"),
    token: str  = Query(..., description="Secret token for verification"),
):
    """รับ Reject จาก Discord → ไม่แตะ Vault"""

    if token != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    log.info(f"Rejected run_id={run_id}")
    await notify_discord(
        f"🚫 **ยกเลิกการอัปเดต Vault**\nrun_id: `{run_id}`",
        color=16711680,
        title="Vault Update — Rejected",
    )
    return {"status": "rejected", "run_id": run_id}
