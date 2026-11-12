"""Google sign-in as the connector's front door.

Switched on only when GOOGLE_SSO_CLIENT_ID and GOOGLE_SSO_CLIENT_SECRET are set. With
them absent nothing here runs and the server behaves exactly as it did before, which is
what makes this safe to deploy to something people are already using.
"""
