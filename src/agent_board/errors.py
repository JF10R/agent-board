"""Domain failures shared by storage, commands and HTTP adapters."""


class BoardError(RuntimeError):
    """An expected board contract or runtime failure."""


class RevisionConflict(BoardError):
    """A mutation was rejected because its observed state is no longer current."""


class TicketConflict(BoardError):
    """A ticket mutation conflicts with its current ownership or lifecycle."""


class CommitUncertain(BoardError):
    """The write may be durable; callers must inspect state before retrying."""

    def __init__(self, ticket_id: str, revision: int):
        self.ticket_id = ticket_id
        self.revision = revision
        super().__init__(
            f"commit outcome uncertain for {ticket_id} at revision {revision}; "
            "inspect the ticket before retrying"
        )
