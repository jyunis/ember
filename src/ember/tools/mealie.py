"""Mealie tools.

REST wrappers around the local Mealie instance. Auth via `Authorization:
Bearer <API key>` from env. Six tools cover the conversational UX:

- `mealie_search` — full-text search across recipes.
- `mealie_random` — pick one or more random recipes (optionally tag-filtered).
- `mealie_get_recipe` — full recipe by slug.
- `mealie_add_recipe` — Mealie's URL scraper imports a new recipe.
- `mealie_create_recipe` — write a recipe from scratch (name, ingredients,
  steps, tags, …) — no URL needed.
- `mealie_update_recipe` — edit fields on an existing recipe by slug.

Write semantics (verified against Mealie v3.24.0, 2026-09-08):

- `POST /api/recipes {name}` creates a bare recipe and returns its slug as
  a bare JSON string; everything else is applied with a follow-up
  `PATCH /api/recipes/{slug}`.
- `PATCH` merges at the top level — omitted keys keep their value — but
  list fields (`recipeIngredient`, `recipeInstructions`, `tags`) are
  replaced wholesale. Callers editing a list must send the whole list.
- Tags given as `{name, slug}` without an `id` are auto-created by Mealie;
  we pre-resolve by name so an existing tag isn't duplicated under a
  differently-derived slug.
- Renaming changes the slug. `PATCH` returns the full recipe, so we report
  the post-update slug back to the model.

Confirmation: none of the Mealie tools gate on `requires_confirmation`,
including the three that write (add/create/update). Owner decision
2026-09-08 — recipe writes are low-stakes and easily reverted in Mealie's
UI, and a 🔒 prompt on every "save this recipe" was friction, not safety.
`summarize_action` stays on the write tools for log/readability purposes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from ember.errors import ToolError, ToolRetryable
from ember.tools.base import ToolResult

_log = logging.getLogger("ember.tools.mealie")

_DEFAULT_TIMEOUT = 30.0


class _MealieClient:
    """Shared base — holds base_url + api_key, builds auth headers."""

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=self._headers(),
                )
        except httpx.ConnectError as e:
            raise ToolRetryable(f"mealie unreachable: {e}") from e
        except httpx.HTTPError as e:
            raise ToolError(f"mealie HTTP error: {e}") from e
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response, what: str) -> None:
        if resp.status_code in (401, 403):
            raise ToolError(f"mealie {what}: auth rejected (HTTP {resp.status_code})")
        if resp.status_code == 404:
            raise ToolError(f"mealie {what}: not found")
        if resp.status_code >= 500:
            raise ToolError(f"mealie {what}: server error (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise ToolError(f"mealie {what}: HTTP {resp.status_code}: {resp.text[:200]}")


# --- Recipe write helpers -----------------------------------------------------

_WRITE_FIELDS: dict[str, dict[str, Any]] = {
    # Friendly arg name → JSON-schema fragment. Shared by create + update so
    # the two tools accept the same vocabulary.
    "description": {"type": "string", "description": "Short description / blurb."},
    "ingredients": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Ingredient lines as plain text, e.g. '2 cups flour'. One string per ingredient."
        ),
    },
    "instructions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Step texts, in order. One string per step.",
    },
    "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tag names (e.g. 'dinner', 'vegetarian'). Missing tags are created.",
    },
    "servings": {"type": "number", "description": "Number of servings."},
    # NB: not `yield` — the MCP bridge turns property names into Python
    # parameter names, and keywords crash the bridge at startup.
    "recipe_yield": {"type": "string", "description": "Free-text yield, e.g. '12 muffins'."},
    "prep_time": {"type": "string", "description": "Free text, e.g. '15 minutes'."},
    "cook_time": {"type": "string", "description": "Free text, e.g. '45 minutes'."},
    "total_time": {"type": "string", "description": "Free text, e.g. '1 hour'."},
    "source_url": {"type": "string", "description": "Where the recipe came from, if anywhere."},
}

_SCALAR_MAP: dict[str, str] = {
    "name": "name",
    "description": "description",
    "servings": "recipeServings",
    "recipe_yield": "recipeYield",
    "prep_time": "prepTime",
    "cook_time": "cookTime",
    "total_time": "totalTime",
    "source_url": "orgURL",
}


def _slugify(text: str) -> str:
    """Mealie-compatible enough slug: lowercase, non-alphanumerics → '-'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "tag"


class _MealieWriter(_MealieClient):
    """Shared plumbing for the two recipe-writing tools."""

    async def _resolve_tags(self, names: list[str]) -> list[dict[str, Any]]:
        """Map tag names → Mealie tag objects.

        Existing tags (case-insensitive name match) are reused with their
        id so Mealie doesn't mint a duplicate under a different slug. Unknown
        tags are passed as `{name, slug}`; Mealie creates them on PATCH.
        """
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in names:
            name = raw.strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            resp = await self._request(
                "GET", "/api/organizers/tags", params={"search": name, "perPage": 25}
            )
            match: dict[str, Any] | None = None
            if resp.status_code < 400:
                body = resp.json()
                items = body.get("items", []) if isinstance(body, dict) else body
                for it in items:
                    if isinstance(it, dict) and str(it.get("name", "")).lower() == name.lower():
                        match = it
                        break
            if match is not None:
                out.append({"id": match.get("id"), "name": match["name"], "slug": match["slug"]})
            else:
                out.append({"name": name, "slug": _slugify(name)})
        return out

    async def _build_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Translate friendly args into a Mealie `Recipe` PATCH body."""
        body: dict[str, Any] = {}
        for arg, key in _SCALAR_MAP.items():
            if arg in args and args[arg] is not None:
                body[key] = args[arg]
        if "ingredients" in args:
            body["recipeIngredient"] = [
                {"note": line.strip()} for line in args["ingredients"] if line.strip()
            ]
        if "instructions" in args:
            body["recipeInstructions"] = [
                {"text": step.strip()} for step in args["instructions"] if step.strip()
            ]
        if "tags" in args:
            body["tags"] = await self._resolve_tags(args["tags"])
        return body

    async def _patch(self, slug: str, body: dict[str, Any], what: str) -> dict[str, Any]:
        resp = await self._request("PATCH", f"/api/recipes/{slug}", json=body)
        self._raise_for_status(resp, what)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _summarize_fields(args: dict[str, Any]) -> list[str]:
        parts: list[str] = []
        if "ingredients" in args:
            parts.append(f"{len(args['ingredients'])} ingredients")
        if "instructions" in args:
            parts.append(f"{len(args['instructions'])} steps")
        if "tags" in args:
            parts.append("tags: " + ", ".join(args["tags"]))
        for arg in (
            "description",
            "servings",
            "recipe_yield",
            "prep_time",
            "cook_time",
            "total_time",
        ):
            if arg in args:
                parts.append(f"{arg}={args[arg]!r}")
        if "source_url" in args:
            parts.append(f"source {args['source_url']}")
        return parts


class MealieSearchTool(_MealieClient):
    name: str = "mealie_search"
    description: str = "Search the Mealie recipe collection. Returns recipe slugs + names."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search text."},
            "limit": {"type": "integer", "default": 10, "description": "Max results."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        query = args["query"]
        limit = int(args.get("limit", 10))
        resp = await self._request(
            "GET",
            "/api/recipes",
            params={"search": query, "perPage": limit},
        )
        self._raise_for_status(resp, "search")
        body = resp.json()
        # Pass through the items list; trim to (slug, name, description) for compactness.
        items = body.get("items", []) if isinstance(body, dict) else body
        trimmed = [
            {
                "slug": it.get("slug"),
                "name": it.get("name"),
                "description": it.get("description"),
            }
            for it in items
            if isinstance(it, dict)
        ]
        return ToolResult(output={"query": query, "count": len(trimmed), "items": trimmed})


class MealieRandomTool(_MealieClient):
    name: str = "mealie_random"
    description: str = "Return one or more random recipes, optionally filtered by tag."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "default": 1},
            "tag": {"type": "string", "description": "Optional tag slug to filter by."},
        },
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        # Mealie 2.x removed `/api/recipes/random` in favor of the
        # paginated list endpoint with `orderBy=random` + a required
        # `paginationSeed`. One request returns up to `perPage` items;
        # use the wall clock as the seed so each call yields a fresh
        # ordering.
        count = max(1, int(args.get("count", 1)))
        tag = args.get("tag")
        params: dict[str, Any] = {
            "orderBy": "random",
            "paginationSeed": str(int(time.time() * 1000)),
            "perPage": count,
        }
        if tag:
            params["queryFilter"] = f'tags.slug = "{tag}"'

        resp = await self._request("GET", "/api/recipes", params=params)
        self._raise_for_status(resp, "random")
        body = resp.json()
        items = body.get("items", []) if isinstance(body, dict) else []
        recipes = [
            {
                "slug": r.get("slug"),
                "name": r.get("name"),
                "description": r.get("description"),
            }
            for r in items
        ]
        return ToolResult(output={"count": len(recipes), "items": recipes})


class MealieGetRecipeTool(_MealieClient):
    name: str = "mealie_get_recipe"
    description: str = "Fetch a single recipe by its slug. Returns ingredients, instructions, tags."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Recipe slug (URL-style, lowercase-with-dashes).",
            },
        },
        "required": ["slug"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = False

    async def call(self, args: dict[str, Any]) -> ToolResult:
        slug = args["slug"]
        resp = await self._request("GET", f"/api/recipes/{slug}")
        self._raise_for_status(resp, "get_recipe")
        return ToolResult(output=resp.json())


class MealieAddRecipeTool(_MealieClient):
    name: str = "mealie_add_recipe"
    description: str = (
        "Import a recipe from a URL. Mealie's scraper extracts ingredients + "
        "instructions automatically. Returns the new recipe's slug."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Recipe URL (any reasonable food blog or recipe site).",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = (
        False  # owner decision 2026-09-08: recipe writes are low-stakes, no prompt
    )

    async def call(self, args: dict[str, Any]) -> ToolResult:
        url = args["url"]
        resp = await self._request(
            "POST",
            "/api/recipes/create/url",
            json={"url": url, "include_tags": False},
        )
        self._raise_for_status(resp, "add_recipe")
        body = resp.json()
        # Mealie returns either a slug string or an object with a slug.
        if isinstance(body, str):
            slug = body
        elif isinstance(body, dict):
            slug = body.get("slug") or body.get("id")
        else:
            slug = None
        return ToolResult(output={"url": url, "slug": slug, "raw": body})


class MealieCreateRecipeTool(_MealieWriter):
    name: str = "mealie_create_recipe"
    description: str = (
        "Create a new recipe in Mealie from scratch — no URL required. Use this "
        "for recipes dictated by the user, invented on the spot, or transcribed "
        "from a photo/PDF. Provide the name plus any of: ingredients (one string "
        "per line), instructions (one string per step), tags, servings, times, "
        "description, source_url. Returns the new recipe's slug. To import from "
        "a web page use mealie_add_recipe instead."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Recipe title."},
            **_WRITE_FIELDS,
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = (
        False  # owner decision 2026-09-08: recipe writes are low-stakes, no prompt
    )

    def summarize_action(self, args: dict[str, Any]) -> str:
        parts = self._summarize_fields(args)
        detail = f" ({'; '.join(parts)})" if parts else ""
        return f"Create Mealie recipe {args['name']!r}{detail}"

    async def call(self, args: dict[str, Any]) -> ToolResult:
        name = args["name"].strip()
        if not name:
            raise ToolError("mealie create_recipe: name must not be blank")
        resp = await self._request("POST", "/api/recipes", json={"name": name})
        self._raise_for_status(resp, "create_recipe")
        body = resp.json()
        slug = body if isinstance(body, str) else (body or {}).get("slug")
        if not slug:
            raise ToolError(f"mealie create_recipe: unexpected response {str(body)[:200]}")

        patch = await self._build_patch({k: v for k, v in args.items() if k != "name"})
        if patch:
            try:
                await self._patch(slug, patch, "create_recipe (details)")
            except ToolError as e:
                # The bare recipe exists; tell the model so it can finish
                # with mealie_update_recipe rather than creating a duplicate.
                raise ToolError(
                    f"{e} — recipe {slug!r} was created with only its name; "
                    "use mealie_update_recipe to add the rest"
                ) from e
        return ToolResult(output={"slug": slug, "name": name, "fields_set": sorted(patch.keys())})


class MealieUpdateRecipeTool(_MealieWriter):
    name: str = "mealie_update_recipe"
    description: str = (
        "Edit an existing Mealie recipe by slug. Only the fields you pass are "
        "changed. Lists (ingredients, instructions, tags) REPLACE the whole "
        "list — to add or fix one item, fetch the recipe with mealie_get_recipe "
        "first and send the complete revised list. Renaming changes the slug; "
        "the response includes the new slug."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Slug of the recipe to edit."},
            "name": {"type": "string", "description": "New title (changes the slug)."},
            **_WRITE_FIELDS,
        },
        "required": ["slug"],
        "additionalProperties": False,
    }
    requires_confirmation: bool = (
        False  # owner decision 2026-09-08: recipe writes are low-stakes, no prompt
    )

    def summarize_action(self, args: dict[str, Any]) -> str:
        parts = self._summarize_fields(args)
        if "name" in args:
            parts.insert(0, f"rename to {args['name']!r}")
        detail = "; ".join(parts) if parts else "no changes"
        return f"Update Mealie recipe {args['slug']!r}: {detail}"

    async def call(self, args: dict[str, Any]) -> ToolResult:
        slug = args["slug"]
        fields = {k: v for k, v in args.items() if k != "slug"}
        if not fields:
            raise ToolError("mealie update_recipe: pass at least one field to change")
        patch = await self._build_patch(fields)
        data = await self._patch(slug, patch, "update_recipe")
        new_slug = data.get("slug") or slug
        return ToolResult(
            output={
                "slug": new_slug,
                "previous_slug": slug,
                "name": data.get("name"),
                "fields_updated": sorted(patch.keys()),
            }
        )
