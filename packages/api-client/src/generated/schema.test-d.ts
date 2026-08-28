/**
 * Type-level pins of the generated multipart upload surface (Child 7 spec 5).
 *
 * These assertions run through `tsc --noEmit` (the `type-check` script): the
 * generated `schema.ts` must expose exactly the five multipart route entries
 * with their semantic operation ids' response shapes, the part-URL data as
 * the sole URL-bearing multipart model, and no private staging/provider
 * identity member on any multipart response schema.
 */

import { expectTypeOf } from "vitest";
import type { components, paths } from "./schema";

type Schemas = components["schemas"];

type MultipartPaths = Extract<
  keyof paths,
  | "/api/uploads/multipart-sessions"
  | "/api/uploads/multipart-sessions/{session_id}"
  | "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url"
  | "/api/uploads/multipart-sessions/{session_id}/complete"
  | "/api/uploads/multipart-sessions/{session_id}/abort"
>;

expectTypeOf<MultipartPaths>().toEqualTypeOf<
  | "/api/uploads/multipart-sessions"
  | "/api/uploads/multipart-sessions/{session_id}"
  | "/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url"
  | "/api/uploads/multipart-sessions/{session_id}/complete"
  | "/api/uploads/multipart-sessions/{session_id}/abort"
>();

// Every route's success payload is the canonical envelope over its safe data.
expectTypeOf<
  paths["/api/uploads/multipart-sessions"]["post"]["responses"][200]["content"]["application/json"]
>().toEqualTypeOf<Schemas["ApiEnvelope_MultipartSessionPlanData_"]>();
expectTypeOf<
  paths["/api/uploads/multipart-sessions/{session_id}"]["get"]["responses"][200]["content"]["application/json"]
>().toEqualTypeOf<Schemas["ApiEnvelope_MultipartSessionStatusData_"]>();
expectTypeOf<
  paths["/api/uploads/multipart-sessions/{session_id}/parts/{part_number}/url"]["post"]["responses"][200]["content"]["application/json"]
>().toEqualTypeOf<Schemas["ApiEnvelope_MultipartPartUrlData_"]>();
expectTypeOf<
  paths["/api/uploads/multipart-sessions/{session_id}/complete"]["post"]["responses"][200]["content"]["application/json"]
>().toEqualTypeOf<Schemas["ApiEnvelope_MultipartCompletionData_"]>();
expectTypeOf<
  paths["/api/uploads/multipart-sessions/{session_id}/abort"]["post"]["responses"][200]["content"]["application/json"]
>().toEqualTypeOf<Schemas["ApiEnvelope_MultipartSessionStatusData_"]>();

// The part-URL data is the one multipart model carrying a url member.
expectTypeOf<Schemas["MultipartPartUrlData"]>().toHaveProperty("url");
expectTypeOf<Schemas["MultipartPartUrlData"]>().toHaveProperty("expires_at");
expectTypeOf<Schemas["MultipartPartUrlData"]>().toHaveProperty("part_number");
expectTypeOf<Schemas["MultipartPartUrlData"]>().toHaveProperty("offset_bytes");
expectTypeOf<Schemas["MultipartPartUrlData"]>().toHaveProperty("size_bytes");
expectTypeOf<Schemas["MultipartSessionPlanData"]>().not.toHaveProperty("url");
expectTypeOf<Schemas["MultipartSessionStatusData"]>().not.toHaveProperty("url");
expectTypeOf<Schemas["MultipartCompletionData"]>().not.toHaveProperty("url");

// No private staging or provider identity member crosses the wire contract.
expectTypeOf<Schemas["MultipartSessionPlanData"]>().not.toHaveProperty("staging_key");
expectTypeOf<Schemas["MultipartSessionPlanData"]>().not.toHaveProperty("provider_upload_id");
expectTypeOf<Schemas["MultipartSessionStatusData"]>().not.toHaveProperty("staging_key");
expectTypeOf<Schemas["MultipartSessionStatusData"]>().not.toHaveProperty("provider_upload_id");
expectTypeOf<Schemas["MultipartCompletionData"]>().not.toHaveProperty("staging_key");
expectTypeOf<Schemas["MultipartCompletionData"]>().not.toHaveProperty("provider_upload_id");
expectTypeOf<Schemas["MultipartPartUrlData"]>().not.toHaveProperty("staging_key");
expectTypeOf<Schemas["MultipartPartUrlData"]>().not.toHaveProperty("provider_upload_id");
