/**
 * JobPilot inbound email Worker.
 *
 * Cloudflare Email Routing hands this Worker every message sent to the configured
 * domain. It streams the raw RFC822 message to the app rather than parsing it here:
 * Python's email module already handles nested MIME and message/rfc822 attachments,
 * so keeping the Worker dumb means one parser to maintain, and "forward as
 * attachment" backfills work through the same path as ordinary forwards.
 *
 * Deploy:
 *   1. Cloudflare dashboard > Workers & Pages > Create Worker, paste this in
 *   2. Settings > Variables:
 *        APP_URL        = https://your-app-host        (no trailing slash)
 *        INBOUND_SECRET = same value as INBOUND_SECRET in the app's .env
 *   3. Email > Email Routing > Routes > catch-all > send to this Worker
 */

export default {
  async email(message, env, ctx) {
    // Cap what we read. Cloudflare allows large messages and a runaway attachment
    // would blow the Worker's memory before the app ever sees it.
    const MAX_BYTES = 8 * 1024 * 1024;

    let raw;
    try {
      raw = await new Response(message.raw).arrayBuffer();
    } catch (err) {
      console.error("could not read message", err);
      return;
    }

    if (raw.byteLength > MAX_BYTES) {
      console.warn(`dropping oversized message (${raw.byteLength} bytes) for ${message.to}`);
      return;
    }

    const body = JSON.stringify({
      to: message.to,
      from: message.from,
      raw: new TextDecoder("utf-8").decode(raw),
    });

    try {
      const res = await fetch(`${env.APP_URL}/inbound/email`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-inbound-secret": env.INBOUND_SECRET,
        },
        body,
      });
      if (!res.ok) {
        console.error(`app rejected message: ${res.status} ${await res.text()}`);
      }
    } catch (err) {
      console.error("could not reach app", err);
    }
  },
};
