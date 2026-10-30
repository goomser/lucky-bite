# Lucky Bite worker

A ~100-line Cloudflare Worker that (1) keeps the Google key out of the public page, (2) caches Google
answers for everyone for a week, and (3) hosts the spin counter. Free tier covers it comfortably.

## Deploy (first time, ~10 minutes)

You need Node.js installed (nodejs.org, the LTS version). Then, in this `worker` folder:

```
npx wrangler login                      # opens a browser; approve
npx wrangler kv namespace create CACHE  # prints an id; paste it into wrangler.toml where it says PASTE_KV_ID_HERE
npx wrangler secret put GOOGLE_KEY      # paste your Places API key when prompted (it's stored encrypted, never in code)
npx wrangler deploy                     # prints your worker's URL, like https://lucky-bite.<you>.workers.dev
```

Then put that URL into `index.html` as `WORKER_URL` and push. The app will route Google lookups and the
counter through the worker. If `WORKER_URL` is blank, the app falls back to calling Google directly
with the key in the page, exactly as before.

## Afterwards

- Change the code → `npx wrangler deploy` again (seconds).
- See live requests → `npx wrangler tail`.
- Dashboard: dash.cloudflare.com → Workers & Pages → lucky-bite (requests, errors, KV contents).
- The Google key restriction can now be tightened from "websites" to "none needed": the key is only
  ever used server-side by the worker. Keep the API restriction (Places API (New) only).
