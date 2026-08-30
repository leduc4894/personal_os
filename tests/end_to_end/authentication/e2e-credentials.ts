/**
 * The shared credential fixtures of the browser acceptance journeys: the one
 * canonical operator username and the single accepted-login password every
 * spec's login and re-authentication surfaces type. Each value has exactly
 * one definition here; the spec files keep their per-suite spelling by
 * importing this module instead of restating the literal.
 */

/** The canonical operator username every accepted journey signs in with. */
export const E2E_LOGIN_USERNAME = "owner";

/** The one password the login and re-authentication surfaces accept. */
export const E2E_ACCEPTED_LOGIN_PASSWORD = "correct horse battery staple!";
