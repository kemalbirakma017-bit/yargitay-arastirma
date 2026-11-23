"""Run PR code as a local HTTP MCP server and make bounded live Bedesten checks."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

from fastmcp import Client

URL = "http://127.0.0.1:8000/mcp"
DOC_ID = "1236052900"


async def exercise_mcp() -> dict:
    async with Client(URL) as client:
        tools = {tool.name for tool in await client.list_tools()}
        assert {"yargitay_karar_ara", "yargitay_karar_metni"} <= tools, tools

        for division in (
            "Hukuk Genel Kurulu",
            "Ceza Genel Kurulu",
            "8. Hukuk Dairesi",
        ):
            result = await client.call_tool(
                "yargitay_karar_ara",
                {"daire": division, "sayfa_boyutu": 3},
            )
            data = result.data
            assert isinstance(data, dict), type(data).__name__
            decisions = data.get("kararlar") or []
            assert decisions, (division, data.get("toplam_sonuc"))
            assert all(item.get("daire") == division for item in decisions), decisions
            print("DIVISION_FILTER_OK", division, len(decisions), data.get("toplam_sonuc"))

        by_esas = await client.call_tool(
            "yargitay_karar_ara",
            {"daire": "Hukuk Genel Kurulu", "esas_yil": "2025",
             "esas_ilk_sira_no": "710", "sayfa_boyutu": 5},
        )
        esas_data = by_esas.data
        assert isinstance(esas_data, dict), type(esas_data).__name__
        assert any(str(item.get("id")) == DOC_ID for item in esas_data.get("kararlar", [])), esas_data
        print("ESAS_FILTER_OK", esas_data.get("toplam_sonuc"), DOC_ID)

        by_karar = await client.call_tool(
            "yargitay_karar_ara",
            {"daire": "Hukuk Genel Kurulu", "karar_yil": "2026",
             "karar_ilk_sira_no": "626", "sayfa_boyutu": 5},
        )
        karar_data = by_karar.data
        assert isinstance(karar_data, dict), type(karar_data).__name__
        assert any(str(item.get("id")) == DOC_ID for item in karar_data.get("kararlar", [])), karar_data
        print("KARAR_FILTER_OK", karar_data.get("toplam_sonuc"), DOC_ID)

        warning = await client.call_tool(
            "yargitay_karar_ara",
            {"daire": "Hukuk Genel Kurulu", "esas_yil": "2025",
             "esas_ilk_sira_no": "710", "esas_son_sira_no": "720",
             "sayfa_boyutu": 5},
        )
        warning_data = warning.data
        assert isinstance(warning_data, dict), type(warning_data).__name__
        notice = warning_data.get("uyari", "")
        assert "esas_son_sira_no" in notice, warning_data
        print("RANGE_WARNING_OK", notice)

        document = await client.call_tool(
            "yargitay_karar_metni", {"karar_id": DOC_ID}
        )
        document_data = document.data
        assert isinstance(document_data, dict), type(document_data).__name__
        text = document_data.get("metin", "")
        assert isinstance(text, str) and len(text) > 200, document_data
        assert document_data.get("kaynak", "").endswith("/" + DOC_ID)
        print("FULLTEXT_OK", len(text), document_data["kaynak"])
        return {"tool_names": sorted(tools), "text_length": len(text)}


def main() -> None:
    env = os.environ.copy()
    env["PORT"] = "8000"
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "server.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )
    error: Exception | None = None
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError(f"FastMCP server exited with code {process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", 8000), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise TimeoutError("FastMCP HTTP server did not start within 30 seconds")
        asyncio.run(exercise_mcp())
    except Exception as exc:  # reported after collecting the server's diagnostic logs
        error = exc
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=5)

    lines = [line for line in (output or "").splitlines() if "yargitay:" in line]
    print("=== SERVER DIAGNOSTIC LOGS ===")
    for line in lines:
        print(line)
    required = (
        "tool yargitay_karar_ara start",
        "bedesten POST start path=/emsal-karar/searchDocuments",
        "bedesten POST done path=/emsal-karar/searchDocuments status=200",
        "bedesten POST start path=/emsal-karar/getDocumentContent",
        "bedesten POST done path=/emsal-karar/getDocumentContent status=200",
    )
    missing = [marker for marker in required if not any(marker in line for line in lines)]
    if error:
        raise error
    if missing:
        raise AssertionError(f"Expected diagnostic log entries missing: {missing}")
    print("DIAGNOSTIC_LOGS_OK")


if __name__ == "__main__":
    main()
