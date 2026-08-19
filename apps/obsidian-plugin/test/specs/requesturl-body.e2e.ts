import * as crypto from "node:crypto";
import * as http from "node:http";
import { browser } from "@wdio/globals";

/**
 * Isolate what Obsidian's `requestUrl` actually transmits for an exact
 * `ArrayBuffer` PUT body: a local echo server hashes the received bytes and
 * the spec compares that digest against the sent digest. Any mismatch here
 * corrupts every small-file upload at the server's digest gate.
 */
describe("requestUrl exact byte body transport", () => {
  it("transmits an ArrayBuffer body byte for byte", async function () {
    this.timeout(60_000);
    const content = "# Test note\n\nUpdated by the live login journey.\n";
    const echoUrl = process.env.E2E_ECHO_URL ?? "http://127.0.0.1:9377/";
    const sentBytes = new TextEncoder().encode(content);
    const sentDigest = crypto.createHash("sha256").update(sentBytes).digest("hex");

    let received: { digest: string; size: number } | null = null;
    const server = http.createServer((request, response) => {
      const chunks: Buffer[] = [];
      request.on("data", (chunk: Buffer) => chunks.push(chunk));
      request.on("end", () => {
        const body = Buffer.concat(chunks);
        received = {
          digest: crypto.createHash("sha256").update(body).digest("hex"),
          size: body.byteLength,
        };
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify(received));
      });
    });
    const isLocalEcho = echoUrl.startsWith("http://127.0.0.1");
    if (isLocalEcho) {
      await new Promise<void>((resolve) => server.listen(9377, "127.0.0.1", resolve));
    }

    try {
      const echoDigest = await browser.executeObsidian(
        async (
          context: { require: (module: string) => unknown },
          url: string,
          text: string,
        ) => {
          const obsidian = context.require("obsidian") as {
            requestUrl: (param: unknown) => Promise<{ status: number; text: string }>;
          };
          const bytes = new TextEncoder().encode(text);
          const body = bytes.buffer.slice(
            bytes.byteOffset,
            bytes.byteOffset + bytes.byteLength,
          ) as ArrayBuffer;
          const result = await obsidian.requestUrl({
            url,
            method: "PUT",
            headers: { "content-type": "application/octet-stream" },
            throw: false,
            body,
          });
          return { status: result.status, text: result.text } as {
            status: number;
            text: string;
          };
        },
        echoUrl,
        content,
      );
      console.log("REQUESTURL_RAW", JSON.stringify(echoDigest).slice(0, 300));
      const parsed = JSON.parse(echoDigest.text) as { digest: string; size: number };
      const matches = parsed.digest === sentDigest && parsed.size === sentBytes.byteLength;
      console.log(
        "REQUESTURL_BODY_RESULT",
        JSON.stringify({
          sentDigest: sentDigest,
          sentSize: sentBytes.byteLength,
          received: parsed,
          matches,
        }),
      );
      expect(matches).toBe(true);
    } finally {
      server.close();
    }
  });
});
