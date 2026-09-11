"""Repository protocols and their SQLAlchemy implementations.

Every method takes ``user_id`` as its first argument (master §4.2, carry-over
C1): there is no method that can reach a row without an owner, so cross-user
access is impossible by construction rather than by router discipline.
"""
