"""Join the existing SQLite writer transaction without committing its caller."""
from contextlib import contextmanager


@contextmanager
def atomic(db):
    """Only the outer operation owns commit/rollback, including grant consumption."""
    if db.in_transaction:
        yield
    else:
        db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
