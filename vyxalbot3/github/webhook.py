import re
from collections.abc import Callable
from functools import wraps
from logging import getLogger
from pprint import pformat
from typing import AsyncGenerator, Awaitable

from aiohttp.web import Request, Response
from gidgethub import BadRequest, ValidationFailure
from gidgethub.routing import Router
from gidgethub.sansio import Event
from sechat import Room

from vyxalbot3.github.formatters import *

class GitHubWebhookReporter:
    logger = getLogger("GitHubWebhook")
    router = Router()

    @staticmethod
    def handler(
        func: Callable[[Event, Room], AsyncGenerator[str | tuple[str, int], int]]
    ) -> Callable[[Event, "GitHubWebhookReporter"], Awaitable[None]]:
        @wraps(func)
        async def _wrapper(event: Event, self: "GitHubWebhookReporter"):
            generator = func(event, self.room)
            message = await anext(generator)
            while True:
                try:
                    if isinstance(message, str):
                        message_id = await self.room.send(message)
                    else:
                        message_id = await self.room.send(*message)
                    message = await generator.asend(message_id)
                except StopAsyncIteration:
                    break

        return _wrapper

    def __init__(self, room: Room, webhook_secret: str, ignored_repositories: set[str]):
        self.room = room
        self.webhook_secret = webhook_secret
        self.ignored_repositories = ignored_repositories

    async def handle_request(self, request: Request) -> Response:
        try:
            event = Event.from_http(
                request.headers, await request.read(), secret=self.webhook_secret
            )
        except (BadRequest, ValidationFailure):
            return Response(status=400)
        assert isinstance(event.data, dict)
        if repository := event.data.get("repository", False):
            if (
                repository["visibility"] == "private"
                or repository["name"] in self.ignored_repositories
            ):
                return Response(status=200)
        try:
            await self.router.dispatch(event, self)
        except Exception:
            self.logger.exception(
                f"Failed to handle event {event.delivery_id} with payload:\n{pformat(event.data)}"
            )
            return Response(status=500)
        return Response(status=200)

    @router.register("push")
    @handler
    @staticmethod
    async def on_push(event: Event, room: Room):
        if event.data["ref"].split("/")[1] != "heads":
            return
        repository = repository_link(event.data["repository"])
        ref = ref_link("/".join(event.data["ref"].split("/")[2:]), event.data["repository"])
        verb = "force-pushed" if event.data["forced"] else "pushed"
        pusher_name = event.data["pusher"]["name"]
        if pusher_name == event.data["sender"]["login"]:
            sender = user_link(event.data["sender"])
        else:
            sender = pusher_name

        commits = list(filter(lambda commit: commit["distinct"], event.data["commits"]))
        if len(commits) <= 5:
            for commit in commits:
                if len(commit["message"]) > 0:
                    message = commit["message"].splitlines()[0]
                else:
                    message = "(no title)"
                yield f"{sender} {verb} a [commit]({commit["url"]}) to {ref} in {repository}: {message}"
        else:
            if len(commits[-1]["message"]) > 0:
                message = commits[-1]["message"].splitlines()[0]
            else:
                message = "(no title)"
            yield f"{sender} {verb} {len(commits)} commits to {ref} in {repository}: {message}"
    
    @router.register("issues")
    @handler
    @staticmethod
    async def on_issue(event: Event, room: Room):
        issue = issue_link(event.data["issue"])
        sender = user_link(event.data["sender"])
        repository = repository_link(event.data["repository"])
        match event.data["action"]:
            case "assigned" | "unassigned":
                assignee = event.data["assignee"]
                yield f"{sender} {event.data["action"]} {user_link(assignee)} to issue {issue} in {repository}"
                if assignee["login"] == event.data["sender"]["login"]:
                    yield "https://i.stack.imgur.com/1VzAJ.jpg"
            case "closed":
                yield f"{sender} closed issue {issue} as {event.data["issue"]["state_reason"]} in {repository}"
            case "opened" | "reopened":
                yield f"{sender} {event.data["action"]} issue {issue} in {repository}"

    @router.register("pull_request")
    @handler
    @staticmethod
    async def on_pull_request(event: Event, room: Room):
        pr = issue_link(event.data["pull_request"])
        sender = user_link(event.data["sender"])
        repository = repository_link(event.data["repository"])
        match event.data["action"]:
            case "assigned":
                assignee = event.data["assignee"]
                yield f"{sender} assigned {assignee} to pull request {pr} in {repository}"
            case "unassigned":
                assignee = event.data["assignee"]
                yield f"{sender} unassigned {assignee} from pull request {pr} in {repository}"
            case "closed":
                yield (
                    f"{sender} {"merged" if event.data["pull_request"]["merged"] else "closed"} "
                    f"pull request {pr} in {repository}"
                )
            case "ready_for_review":
                yield f"{sender} marked pull request {pr} in {repository} as ready for review"
            case "opened" | "reopened" | "enqueued":
                yield f"{sender} pull request {event.data["action"]} {pr} in {repository}"

    @router.register("pull_request_review", action="submitted")
    @handler
    @staticmethod
    async def on_review_submitted(event: Event, room: Room):
        sender = user_link(event.data["sender"])
        repository = repository_link(event.data["repository"])
        pr = issue_link(event.data["pull_request"])
        review = event.data["review"]
        match review["state"]:
            case "commented":
                if not len(review["body"]):
                    return
                action = "commented on"
            case "approved":
                action = "approved"
            case "changes_requested":
                action = "requested changes on"
            case _:
                return
        yield (
            f"{sender} [{action}]({review["html_url"]}) {pr} in {repository}"
            f"{f": \"{escape_markdown(review["body"].splitlines()[0])}\"" if len(review["body"]) else ""}"
        )
    
    @router.register("create")
    @router.register("delete")
    @handler
    @staticmethod
    async def on_ref_change(event: Event, room: Room):
        sender = user_link(event.data["sender"])
        repository = repository_link(event.data["repository"])
        yield f"{sender} {event.event}d {event.data["ref_type"]} {event.data["ref"]} in {repository}"

    @router.register("release", action="released")
    @handler
    @staticmethod
    async def on_release(event: Event, room: Room):
        release = event.data["release"]
        release_name = release["name"].lower()
        # attempt to match version number, otherwise default to the whole name
        if match := re.search(r"\d.*", release_name):
            release_name = match[0]

        yield f"__[{event.data["repository"]["name"]} {release_name}]({release["html_url"]})__"

    @router.register("fork")
    @handler
    @staticmethod
    async def on_fork(event: Event, room: Room):
        sender = user_link(event.data["sender"])
        repository = repository_link(event.data["repository"])
        forkee = repository_link(event.data["forkee"])
        yield f"{sender} forked {forkee} from {repository}"

    @router.register("repository")
    @handler
    @staticmethod
    async def on_repository(event: Event, room: Room):
        sender = user_link(event.data["sender"])
        repository = repository_link(event.data["repository"])
        yield f"{sender} {event.data["action"]} repository {repository}"