class FakeSession:
    def __init__(self, rows=(), state="finished", run_exists=True):
        self.rows = rows
        self.state = state
        self.run_exists = run_exists
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def scalars(self, statement):
        if "run_events" not in str(statement):
            class EmptyResult:
                def all(self):
                    return []

                def __iter__(self):
                    return iter(())

            return EmptyResult()
        criteria = statement.whereclause.clauses
        cursor = next(
            int(clause.right.value)
            for clause in criteria
            if getattr(clause.left, "name", None) == "id"
            and getattr(clause.operator, "__name__", "") == "gt"
        )
        self.last_limit = statement._limit_clause.value
        limit = self.last_limit

        class Result:
            def __init__(self, rows):
                self.rows = [row for row in rows if row.id > cursor][:limit]

            def all(self):
                return self.rows

        return Result(self.rows)

    async def get(self, model, _identity):
        if model.__name__ == "Run":
            return type("Row", (), {"state": self.state})() if self.run_exists else None
        return None


class FakeSessionmaker:
    def __init__(self, rows=(), state="finished", run_exists=True):
        self.sessions = []
        self.rows = rows
        self.state = state
        self.run_exists = run_exists

    def __call__(self):
        session = FakeSession(self.rows, self.state, self.run_exists)
        self.sessions.append(session)
        return session
