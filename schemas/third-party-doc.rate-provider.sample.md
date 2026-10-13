# Rate Provider API (v1)

Base URL: https://api.rates.example.invalid

Every request must include an API key in the `X-Api-Key` header.

## Endpoints

- GET /rate?base=USD&quote=CNY — returns { base, quote, value }
- POST /subscription — create a rate alert; returns a subscription id

## Errors

- 429 Too Many Requests
- 500 Internal Server Error

## Webhooks

We can send a callback to your server when a subscription triggers.

## Client libraries

We provide an official client library (SDK) you may install.
