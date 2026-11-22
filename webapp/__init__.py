"""Web app for the wife and the family member: a front page with a picture, a
login, and each person's own view, check-in and package. It talks to the
heirdrop service only through its Unix socket and imports nothing from the
`heirdrop` package: the socket protocol (doc/2-heirdrop-service.md, "Local
interface") is the boundary. See doc/4-webapp.md and AGENTS.md "Web app".
"""
