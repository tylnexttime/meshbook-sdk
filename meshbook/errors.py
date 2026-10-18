"""meshbook.errors — the one exception type the SDK raises, and its codes.

Wrappers (the CLI, the MCP, your agent) should branch on
:attr:`MeshbookError.code` and :attr:`MeshbookError.hint`, never on the
wording of :attr:`MeshbookError.message`, which is written for a person
reading a Python traceback and may change between releases. Codes and
hints are stable: a code is never renamed or reused in a minor release,
and :data:`LOCAL_CODES` / :data:`HINTS` list every one this SDK raises
itself (the README's error table is checked against them by a test).
Codes from the server's error envelope pass through unchanged.
"""
from __future__ import annotations

#: Every code the SDK raises itself, with what it means. Server codes
#: (``forbidden``, ``not_found``, ``token_out_of_scope`` …) pass through
#: as the server sent them and are not listed here.
LOCAL_CODES: dict[str, str] = {
    # arguments and local validation
    "bad_argument": "an argument is invalid or unsupported (e.g. offset= where the server pages by before=)",
    "bad_auth": "unknown auth= mode",
    "bad_channel": "empty channel reference",
    "bad_date": "not a date / YYYY-MM-DD",
    "bad_datetime": "not an aware date-time / ISO 8601 string",
    "naive_datetime": "a date-time without a time zone",
    "bad_username": "empty username",
    "file_not_found": "the local file to upload does not exist",
    "empty_file": "the local file to upload is empty",
    # name resolution
    "mesh_not_found": "no mesh of yours matches the id or name",
    "mesh_ambiguous": "several meshes match the name",
    "no_active_mesh": "the call needs a mesh and none was given or configured",
    "user_not_found": "no mesh member matches",
    "user_ambiguous": "several members match the display name",
    "contact_not_found": "no contact matches",
    "contact_ambiguous": "several contacts match",
    "project_not_found": "no project matches",
    "project_ambiguous": "several projects match",
    "channel_not_found": "no channel matches",
    "stage_not_found": "no pipeline stage matches",
    "stage_ambiguous": "the stage name exists in several pipelines",
    "no_pipeline": "the mesh has no pipelines",
    "no_stage": "the pipeline has no stages",
    "not_channel_message": "the message is not in a channel or DM",
    "duplicate_event": "an event with that title already starts then (dedupe guard)",
    # identity and credentials
    "not_authenticated": "no usable credential, or the server rejected it",
    "no_user": "authenticated, but /api/me names no user",
    "key_exists": "an agent key already exists locally; replacing it needs force=True",
    "no_agent_key": "no usable agent key or mint bundle on this machine",
    "bad_agent_key": "the agent private key file cannot be read",
    "pending_key": "an interrupted enroll/register left an unconfirmed key",
    "no_pending_key": "recover_pending() found no pending key",
    "not_enrolled": "the server answered 200 without an enrolled bundle",
    "mint_failed": "the token endpoint did not return an access token",
    "missing_dependency": "the agent lane needs the [agent] extra (cryptography)",
    # config file
    "config_unreadable": "the config file exists but is not valid; it was left untouched",
    "config_write_failed": "the config file or its directory cannot be read or written",
    "config_locked": "another meshbook process holds the config lock",
    # transport
    "network_error": "the request or its response was lost on the network",
    "http_error": "an HTTP error with no machine code in the body",
    "validation_error": "the server's request validation failed (FastAPI 422)",
    "bad_response": "the server answered with a non-JSON body",
    "api_error": "a 200 body carried ok=false with no code",
    "insecure_base": "the base URL is not https (plain http only for localhost)",
    "redirect_refused": "the server redirected off-origin or redirected a write",
    # downloads
    "file_exists": "the download target exists; pass overwrite=True to replace it",
    "unsafe_path": "the download would land outside the target directory",
    "write_failed": "the downloaded file cannot be written",
}

#: The default machine hint for each code: what a caller can DO about it.
#: A raise site may give a more specific one (``bad_argument`` from an
#: offset= refusal carries ``use_before_cursor``). Hints are short stable
#: snake_case tokens with no Python syntax in them, so a wrapper can map
#: them to its own wording (a CLI flag, an MCP tool argument).
HINTS: dict[str, str] = {
    "bad_argument": "fix_argument",
    "bad_auth": "fix_argument",
    "bad_channel": "fix_argument",
    "bad_date": "fix_argument",
    "bad_datetime": "add_timezone",
    "naive_datetime": "add_timezone",
    "bad_username": "fix_argument",
    "file_not_found": "check_local_path",
    "empty_file": "check_local_path",
    "mesh_not_found": "check_name",
    "mesh_ambiguous": "use_id",
    "no_active_mesh": "choose_mesh",
    "user_not_found": "check_name",
    "user_ambiguous": "use_username",
    "contact_not_found": "check_name",
    "contact_ambiguous": "use_id",
    "project_not_found": "check_name",
    "project_ambiguous": "use_id",
    "channel_not_found": "check_name",
    "stage_not_found": "check_name",
    "stage_ambiguous": "use_id",
    "no_pipeline": "create_in_web_app",
    "no_stage": "create_in_web_app",
    "not_channel_message": "reply_in_entity_chat",
    "duplicate_event": "allow_duplicate",
    "not_authenticated": "provide_credentials",
    "no_user": "check_account",
    "key_exists": "confirm_replace_key",
    "no_agent_key": "enroll_agent_key",
    "bad_agent_key": "enroll_agent_key",
    "pending_key": "resolve_pending_key",
    "no_pending_key": "none_needed",
    "not_enrolled": "retry_later",
    "mint_failed": "check_agent_key",
    "missing_dependency": "install_agent_extra",
    "config_unreadable": "fix_config",
    "config_write_failed": "check_permissions",
    "config_locked": "retry_later",
    "network_error": "retry_later",
    "http_error": "check_status",
    "validation_error": "fix_argument",
    "bad_response": "retry_later",
    "api_error": "check_status",
    "insecure_base": "fix_base_url",
    "redirect_refused": "fix_base_url",
    "file_exists": "allow_overwrite",
    "unsafe_path": "choose_out_path",
    "write_failed": "check_permissions",
}

#: Every hint, with the action it stands for (the README table mirrors it).
HINT_MEANINGS: dict[str, str] = {
    "fix_argument": "correct the argument named in the message",
    "add_timezone": "give the date-time a time zone (an offset or tzinfo)",
    "check_local_path": "check the local file path",
    "check_name": "check the name or id; the message lists what exists where it can",
    "use_id": "refer to it by id instead of name",
    "use_username": "refer to the member by @username",
    "choose_mesh": "name a mesh for the call, or set a default mesh",
    "create_in_web_app": "create the missing item in the web app first",
    "reply_in_entity_chat": "reply through entity chat, not a channel",
    "allow_duplicate": "turn the duplicate guard off for this call if a second one is intended",
    "provide_credentials": "supply an API token or sign in",
    "reauthenticate": "the credential was rejected: sign in again or mint a new token",
    "check_account": "check the account's state in the web app",
    "confirm_replace_key": "confirm that the existing agent key may be replaced",
    "enroll_agent_key": "enroll or register an agent key",
    "resolve_pending_key": "recover or discard the pending agent key",
    "none_needed": "nothing to do",
    "check_agent_key": "check the agent key and its mint bundle",
    "install_agent_extra": "install meshbook-sdk[agent]",
    "fix_config": "repair or quarantine the config file named in the message",
    "check_permissions": "check file permissions and free space",
    "retry_later": "retry after a short wait",
    "check_before_retry": "a write may have been applied: check before retrying it",
    "check_status": "see the HTTP status and message",
    "fix_base_url": "use the https apex base URL",
    "allow_overwrite": "allow overwriting the existing file, or choose another path",
    "choose_out_path": "choose another output directory or file name",
    "use_before_cursor": "page with limit and before (no offset on this route)",
    "choose_permitted_mesh": "use a mesh this token is scoped to",
}


class MeshbookError(Exception):
    """Any failure talking to meshbook.org, or refused locally before the wire.

    Attributes:
        status:  HTTP status code (0 for network / local errors).
        code:    stable machine code: from the API error envelope (e.g.
                 ``forbidden``, ``not_found``) or a local one listed in
                 :data:`LOCAL_CODES` (``mesh_ambiguous``, ``file_exists`` …).
        message: human-readable message, safe to show to a person or a
                 model; its wording is NOT stable, so do not parse it.
        hint:    stable machine hint for what to do next (see
                 :data:`HINT_MEANINGS`), or None when there is no generic
                 advice. Defaults from :data:`HINTS` by code; a 401 with
                 no mapped code gets ``reauthenticate``.
    """

    def __init__(self, code: str, message: str, status: int = 0, *, hint: str | None = None):
        self.code = code
        self.message = message
        self.status = status
        if hint is None:
            hint = HINTS.get(code)
            if hint is None and status == 401:
                hint = "reauthenticate"
            elif hint is None and code == "token_out_of_scope":
                hint = "choose_permitted_mesh"
        self.hint = hint
        super().__init__(f"[{status}] {code}: {message}")


__all__ = ["HINTS", "HINT_MEANINGS", "LOCAL_CODES", "MeshbookError"]
