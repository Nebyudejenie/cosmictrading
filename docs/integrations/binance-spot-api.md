# Binance Spot API — verified facts for the sandbox adapter

- **Reviewed:** 2026-10-14. Extracted from Binance's official documentation, then independently re-checked claim by claim against the raw files of the official repository `binance/binance-spot-api-docs` (master). `developers.binance.com` returned empty pages to automated access. The testnet CHANGELOG was last updated 2026-10-07.
- **Result:** 80 facts confirmed verbatim, 2 corrected by the checker, 0 unverifiable.
- **Use:** the adapter (`cosmic.trading.brokers.binance`) and its emulator follow these facts. Re-review before any live use; the documentation changes.
- **Added 2026-10-30 (§12, for ADR-034):** the key-permission endpoint, the account and book-ticker responses, and Binance's key rules. Read from the official legacy wallet documentation page, the raw `rest-api.md` on master (CHANGELOG "Last Updated: 2026-10-30") and the Binance FAQ. Re-check before the live key check runs against the venue (S40b).

## Confirmed

### 0. Source note

developers.binance.com doc pages redirect to /en/docs/products/spot/... and return HTTP 202 with an empty body. Re-checked on 2026-10-14: GET https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information ended at https://developers.binance.com/en/docs/products/spot/rest-api#general-api-information with HTTP 202 and 0 bytes. Every quote was re-checked against the raw master files at raw.githubusercontent.com/binance/binance-spot-api-docs/master, with whitespace ignored. The testnet CHANGELOG says 'Last Updated: 2026-10-07'.

> **Last Updated: 2026-10-07**

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/CHANGELOG.md>

Note: All 81 extracted quotes were found verbatim (whitespace-normalised) in the cited files.

### 1. Base URLs - production REST

The production REST base endpoints are api.binance.com, api-gcp.binance.com and api1 to api4.binance.com. api1 to api4 should perform better but are less stable. For APIs that only send public market data, use https://data-api.binance.vision.

> * The following base endpoints are available. Please use whichever works best for your setup:
>   * **https://api.binance.com**
>   * **https://api-gcp.binance.com**
>   * **https://api1.binance.com**
>   * **https://api2.binance.com**
>   * **https://api3.binance.com**
>   * **https://api4.binance.com**
> * The last 4 endpoints in the point above (`api1`-`api4`) should give better performance but have less stability.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-api-information>

Note: Also verbatim: '* For APIs that only send public market data, please use the base endpoint **https://data-api.binance.vision**.'

### 1. Base URLs - testnet REST

The testnet REST base endpoint is https://testnet.binance.vision/api. testnet/general-info.md also lists https://api1.testnet.binance.vision/api.

> * The base endpoint is **https://testnet.binance.vision/api**

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md#general-api-information>

### 1. Base URLs - testnet URL mapping

The official mapping lists two testnet REST URLs: testnet.binance.vision/api and api1.testnet.binance.vision/api.

> | https://api.binance.com/api<br>https://api-gcp.binance.com/api<br>https://api1.binance.com/api<br>https://api2.binance.com/api<br>https://api3.binance.com/api<br>https://api4.binance.com/api | **https://testnet.binance.vision/api**<br>**https://api1.testnet.binance.vision/api** |

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

### 1. /sapi on testnet and available /api groups

/sapi is not available on the Spot Test Network. Only /api is: market data, trading and account endpoints over REST and the WebSocket API, plus market streams and user data streams.

> No, only the `/api` endpoints are available on the Spot Test Network:
> 
> * [Market Data Endpoints (REST API)](rest-api.md#market-data-endpoints)
> * [Market Data Requests (WebSocket API)](web-socket-api.md#market-data-requests)
> * [WebSocket Market Streams](web-socket-streams.md)
> * [Trading Endpoints (REST API)](rest-api.md#trading-endpoints)
> * [Account Endpoints (REST API)](rest-api.md#account-endpoints)
> * [Trading Requests (WebSocket API)](web-socket-api.md#trading-requests)
> * [Account Requests (WebSocket API)](web-socket-api.md#account-requests)
> * [User Data Streams](user-data-stream.md)

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

### 2. Signing - SIGNED endpoints general

Every endpoint with a security type other than NONE is SIGNED, which means it carries a 'signature'.

> * Except for `NONE`, all endpoints with a security type are considered `SIGNED` requests (i.e. including a `signature`).

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#request-security>

### 2. Signing - signature parameter location

The signature parameter goes in the query string or the request body.

> * `SIGNED` endpoints require an additional parameter, `signature`, to be sent in the `query string` or `request body`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#signed-endpoint-security>

### 2. Signing - HMAC payload

The signature payload is the query string concatenated with the HTTP body, with no separator. Percent-encode any non-ASCII character before signing. The Ed25519 section uses the same sentence.

> The signature payload of your request is the query string concatenated without separator to the HTTP body. Any non-ASCII character must be percent-encoded before signing.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#hmac-keys>

### 2. Signing - HMAC algorithm and encoding

Sign with HMAC-SHA-256 keyed by the secretKey and encode the output as hex. The secretKey and payload are case-sensitive; the resulting signature is case-insensitive.

> 1. Use the `secretKey` of your API key as the signing key for the HMAC-SHA-256 algorithm.
> 2. Sign the signature payload constructed in Step 1.
> 3. Encode the HMAC-SHA-256 output as a hex string.
> 
> Note that `secretKey` and the payload are **case-sensitive**, while the resulting signature value is case-insensitive.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#hmac-keys>

### 2. Signing - Ed25519 percent-encode step

Step 3 of the Ed25519 procedure is to percent-encode the base64 string. The worked examples show '/' as %2F, '+' as %2B and '=' as %3D. Step 3 of the request is to add the signature parameter to the query string.

> 3. Percent-encode the base64 string.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#ed25519-keys>

### 2. Signing - Ed25519 Python reference (testnet)

The official testnet Python example builds the payload as '&'-joined param=value pairs, timestamp included, with no percent-encoding. The signature is base64(private_key.sign(payload)). The request is sent with requests.post('https://testnet.binance.vision/api/v3/order', headers={'X-MBX-APIKEY': ...}, data=params). Because the raw join is not percent-encoded, it matches the rest-api.md urlencode form only for ASCII-only values; use urlencode to be safe.

> payload = '&'.join([f'{param}={value}' for param, value in params.items()])
> signature = base64.b64encode(private_key.sign(payload.encode('ASCII')))
> params['signature'] = signature

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

### 2. Signing - X-MBX-APIKEY header

Put the API key in the X-MBX-APIKEY header. Ed25519 keys work the same way as the other key types.

> During registration, we will generate an API key for you. Please put it in the `X-MBX-APIKEY` header of your requests, exactly the same way as with other API key types.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

### 2. Signing - case sensitivity

HMAC signatures are not case-sensitive. RSA and Ed25519 signatures are case-sensitive.

> * **HMAC:** Signatures generated using HMAC are **not case-sensitive**. This means the signature string can be verified regardless of letter casing.
> * **RSA:** Signatures generated using RSA are **case-sensitive**.
> * **Ed25519:** Signatures generated using Ed25519 are also **case-sensitive**

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#signature-case-sensitivity>

### 2. Signing - HMAC deprecated

HMAC keys are deprecated. Binance recommends migrating to asymmetric keys (Ed25519 or RSA); rest-api.md calls Ed25519 'highly recommended'.

> **HMAC keys are deprecated.** We recommend to migrate to asymmetric API keys, such as Ed25519 or RSA.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/api_key_types.md>

### 2. timestamp and recvWindow semantics

timestamp is required, in milliseconds or microseconds. recvWindow is optional, in milliseconds only, with up to 3 decimal places. It defaults to 5000 ms and cannot exceed 60000 ms.

> * `SIGNED` requests also require a `timestamp` parameter which should be the current timestamp either in milliseconds or microseconds. (See [General API Information](#general-api-information))
> * An additional optional parameter, `recvWindow`, specifies for how long the request stays valid and may only be specified in milliseconds.
>   * `recvWindow` supports up to three decimal places of precision (e.g., 6000.346) so that microseconds may be specified.
>   * If `recvWindow` is not sent, **it defaults to 5000 milliseconds**.
>   * Maximum `recvWindow` is 60000 milliseconds.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#timing-security>

### 2. timestamp acceptance logic

The request is processed only if timestamp < serverTime + 1 second and serverTime - timestamp <= recvWindow. recvWindow is checked again before the request is forwarded to the Matching Engine.

> if (timestamp < (serverTime + 1 second) && (serverTime - timestamp) <= recvWindow) {
>   // begin processing request
>   serverTime = getCurrentTime()
>   if (serverTime - timestamp) <= recvWindow {
>     // forward request to Matching Engine
>   } else {
>     // reject request
>   }

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#timing-security>

### 2. Error -1021

-1021 INVALID_TIMESTAMP has two messages: one for a timestamp outside recvWindow and one for a timestamp 1000 ms ahead of server time.

> ### -1021 INVALID_TIMESTAMP
>  * Timestamp for this request is outside of the recvWindow.
>  * Timestamp for this request was 1000ms ahead of the server's time.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 2. Error -1022

An invalid signature returns -1022 INVALID_SIGNATURE.

> ### -1022 INVALID_SIGNATURE
>  * Signature for this request is not valid.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 2. Parameter transport

POST, PUT and DELETE accept parameters in the query string or in an application/x-www-form-urlencoded body, and the two can be mixed. If a parameter is in both, the query-string value is used.

> * If a parameter sent in both the `query string` and `request body`, the
>   `query string` parameter will be used.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-information-on-endpoints>

Note: Quote replaced with the precedence sentence, which is what supports 'query string wins'. The extractor's original quote about mixing parameters is also verbatim.

### 3. POST /api/v3/order - weight

POST /api/v3/order has request weight 1 and unfilled order count 1.

> **Weight:**
> 1
> 
> **Unfilled Order Count:**
> 1

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. POST /api/v3/order - required params by type

LIMIT requires timeInForce, quantity and price. STOP_LOSS_LIMIT requires timeInForce, quantity, price, and stopPrice or trailingDelta. Every order also needs symbol, side, type and timestamp.

> `LIMIT` | `timeInForce`, `quantity`, `price`|
> ...
> `STOP_LOSS_LIMIT` | `timeInForce`, `quantity`,  `price`, `stopPrice` or `trailingDelta`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. POST /api/v3/order - stop trigger direction

The trigger-price rules apply to both the MARKET and LIMIT versions. STOP_LOSS SELL triggers below market price; STOP_LOSS BUY triggers above it. errors.md lists the matching-engine message 'Order would trigger immediately.', described as 'The order's stop price is not valid when compared to the last traded price.' The docs do not explicitly tie that message to these rules.

> Trigger order price rules against market price for both MARKET and LIMIT versions:
> 
> * Price above market price: `STOP_LOSS` `BUY`, `TAKE_PROFIT` `SELL`
> * Price below market price: `STOP_LOSS` `SELL`, `TAKE_PROFIT` `BUY`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. newClientOrderId rules

newClientOrderId must be unique among open orders and is generated automatically if not sent. An order reusing it is accepted only after the previous one is filled; otherwise it is rejected. No character regex or maximum length is documented.

> newClientOrderId | STRING | NO | A unique id among open orders. Automatically generated if not sent.<br/> Orders with the same `newClientOrderID` can be accepted only when the previous one is filled, otherwise the order will be rejected.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. newClientOrderId regex/length

NOT DOCUMENTED. No regex or length limit for newClientOrderId appears in rest-api.md, errors.md, faqs/spot_glossary.md, CHANGELOG.md or web-socket-api.md (searched again). The only character class in rest-api.md, [-_.~a-zA-Z0-9], belongs to the percent-encoding helper in the signing shell example, not to clientOrderId. The only related text is the -1100 ILLEGAL_CHARS template.

> ### -1100 ILLEGAL_CHARS
>  * Illegal characters found in a parameter.
>  * Illegal characters found in parameter '%s'; legal range is '%s'.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

Note: Confirms that the item is NOT DOCUMENTED. Do not hard-code a regex as if it were official.

### 3. Duplicate clientOrderId error

The matching-engine message for a clientOrderId already in use is 'Duplicate order sent.'. errors.md lists it in the shared message table for -1010, -2010, -2011 and -2038. No official text pairs it with -2010 on POST /api/v3/order; -2010 is an inference from that table.

> "Duplicate order sent."                                         | The `clOrdId` is already in use.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 3. newOrderRespType values and defaults

newOrderRespType takes ACK, RESULT or FULL. MARKET and LIMIT orders default to FULL. Every other type, STOP_LOSS_LIMIT included, defaults to ACK.

> newOrderRespType | ENUM | NO | Set the response JSON. `ACK`, `RESULT`, or `FULL`; `MARKET` and `LIMIT` order types default to `FULL`, all other orders default to `ACK`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. ACK fields

An ACK response contains only symbol, orderId, orderListId, clientOrderId and transactTime.

> * `newOrderRespType` enum. Stands for a type of order response in JSON where only the following fields are emitted: `symbol`, `orderId`, `orderListId`, `clientOrderId`, and `transactTime`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/spot_glossary.md>

### 3. RESULT fields

RESULT adds to ACK: price, origQty, executedQty, origQuoteOrderQty, cummulativeQuoteQty, status, timeInForce, type, side, workingTime and selfTradePreventionMode. Conditional fields such as stopPrice can also appear.

> "transactTime": 1507725176595,
>     "price": "0.00000000",
>     "origQty": "10.00000000",
>     "executedQty": "10.00000000",
>     "origQuoteOrderQty": "0.000000",
>     "cummulativeQuoteQty": "10.00000000",
>     "status": "FILLED",
>     "timeInForce": "GTC",
>     "type": "MARKET",
>     "side": "SELL",
>     "workingTime": 1507725176595,
>     "selfTradePreventionMode": "NONE"

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. FULL fields

FULL is RESULT plus a 'fills' array. Each fill has price, qty, commission, commissionAsset and tradeId.

> "fills": [
>         {
>             "price": "4000.00000000",
>             "qty": "1.00000000",
>             "commission": "4.00000000",
>             "commissionAsset": "USDT",
>             "tradeId": 56
>         },

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#new-order-trade>

### 3. Conditional order-response fields

stopPrice appears for STOP_LOSS, TAKE_PROFIT, STOP_LOSS_LIMIT and TAKE_PROFIT_LIMIT orders. preventedMatchId and preventedQuantity appear only if the order expired due to STP. expiryReason appears when an order has expired, e.g. 'INSUFFICIENT_LIQUIDITY'.

> `stopPrice`    | Price when the algorithmic order will be triggered | Appears for `STOP_LOSS`. `TAKE_PROFIT`, `STOP_LOSS_LIMIT` and `TAKE_PROFIT_LIMIT` orders.|`"stopPrice": "23500.00000000"`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#conditional-fields-in-order-responses>

### 3. POST /api/v3/order/test

POST /api/v3/order/test checks the signature and recvWindow and validates a new order without sending it to the matching engine. Weight is 1, or 20 with computeCommissionRates (default false). Without computeCommissionRates the response is {}.

> Test new order creation and signature/recvWindow long.
> Creates and validates a new order but does not send it into the matching engine.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#test-new-order-trade>

Note: Weight table, verbatim: '|Without `computeCommissionRates`| 1|' and '|With `computeCommissionRates`|20|'

### 4. GET /api/v3/order - params and weight

Weight 4. symbol and timestamp are mandatory, and orderId or origClientOrderId must be sent. If both are sent, the order is found by orderId and its clientOrderId must then match. Data source: Memory => Database.

> * Either `orderId` or `origClientOrderId` must be sent.
> * If both `orderId` and `origClientOrderId` are provided, the `orderId` is searched first, then the `origClientOrderId` from that result is checked against that order. If both conditions are not met the request will be rejected.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-user_data>

### 4. GET /api/v3/order - response fields

Fields returned: symbol, orderId, orderListId, clientOrderId, price, origQty, executedQty, cummulativeQuoteQty, status, timeInForce, type, side, stopPrice, icebergQty, time, updateTime, isWorking, workingTime, origQuoteOrderQty and selfTradePreventionMode, plus conditional fields. For some historical orders cummulativeQuoteQty is below 0, meaning the data is unavailable.

> "clientOrderId": "myOrder1",
>     "price": "0.1",
>     "origQty": "1.0",
>     "executedQty": "0.0",
>     "cummulativeQuoteQty": "0.0",
>     "status": "NEW",
>     "timeInForce": "GTC",
>     "type": "LIMIT",
>     "side": "BUY",
>     "stopPrice": "0.0",
>     "icebergQty": "0.0",
>     "time": 1499827319559,
>     "updateTime": 1499827319559,
>     "isWorking": true,
>     "workingTime": 1499827319559,
>     "origQuoteOrderQty": "0.000000",
>     "selfTradePreventionMode": "NONE"

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#query-order-user_data>

Note: Also verbatim: '* For some historical orders `cummulativeQuoteQty` will be < 0, meaning the data is not available at this time.'

### 4. Error -2013

-2013 NO_SUCH_ORDER: 'Order does not exist.' Separately, -2039 CLIENT_ORDER_ID_INVALID: 'Client order ID is not correct for this order ID.'

> ### -2013 NO_SUCH_ORDER
>  * Order does not exist.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

Note: -2039, verbatim: '### -2039 CLIENT_ORDER_ID_INVALID\n  * Client order ID is not correct for this order ID.'

### 4. Retention / archiving (-2026)

An order that was CANCELED or EXPIRED with zero executed quantity more than 90 days ago is archived. Querying it returns -2026 ORDER_ARCHIVED; it used to return -2013. EXPIRED_IN_MATCH orders with cumulative quantity 0 are also archived after 90 days. No retention limit is documented for filled orders. Testnet's periodic reset deletes all orders.

> ### -2026 ORDER_ARCHIVED
>   * Order was canceled or expired with no executed qty over 90 days ago and has been archived.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

Note: CHANGELOG.md confirms -2026 replaced -2013 for archived orders: 'Previously, if an archived order (i.e. order with status `CANCELED` or `EXPIRED` where `executedQty` == 0 that occurred more than 90 days in the past.) is queried, the error message would be: ... "code": -2013 ... Now, the error message is: ... "code": -2026'

### 4. Archiving of EXPIRED_IN_MATCH orders

Orders that end in EXPIRED_IN_MATCH with cumulative quantity 0 are archived after 90 days.

> * Orders with cumulative quantity of 0 in the final state `EXPIRED_IN_MATCH` (i.e., the order expired due to STP) will be archived after 90 days.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/CHANGELOG.md>

### 5. DELETE /api/v3/order - params

Weight 1. symbol and timestamp are mandatory, and orderId or origClientOrderId must be sent. Optional: newClientOrderId (identifies this cancel), cancelRestrictions (ONLY_NEW or ONLY_PARTIALLY_FILLED) and recvWindow.

> symbol            | STRING | YES          |
> orderId           | LONG   | NO           |
> origClientOrderId | STRING | NO           |
> newClientOrderId  | STRING | NO           |  Used to uniquely identify this cancel. Automatically generated by default.
> cancelRestrictions| ENUM   | NO           | Supported values: <br>`ONLY_NEW` - Cancel will succeed if the order status is `NEW`.<br> `ONLY_PARTIALLY_FILLED ` - Cancel will succeed if order status is `PARTIALLY_FILLED`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#cancel-order-trade>

### 5. DELETE /api/v3/order - response fields

Response fields: symbol, origClientOrderId, orderId, orderListId, clientOrderId (the cancel's id), transactTime, price, origQty, executedQty, origQuoteOrderQty, cummulativeQuoteQty, status (CANCELED), timeInForce, type, side and selfTradePreventionMode, plus conditional fields.

> "symbol": "LTCBTC",
>     "origClientOrderId": "myOrder1",
>     "orderId": 4,
>     "orderListId": -1, // Unless it's part of an order list, value will be -1
>     "clientOrderId": "cancelMyOrder1",
>     "transactTime": 1684804350068,
>     "price": "2.00000000",
>     "origQty": "1.00000000",
>     "executedQty": "0.00000000",
>     "origQuoteOrderQty": "0.000000",
>     "cummulativeQuoteQty": "0.00000000",
>     "status": "CANCELED",

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#cancel-order-trade>

### 5. Error -2011 on cancel

-2011 CANCEL_REJECTED carries a matching-engine message. The documented message for an order that cannot be found is 'Unknown order sent.'. The docs do NOT say which message comes back when the order exists but is already FILLED or CANCELED, so that case is NOT DOCUMENTED. A failed cancelRestrictions check returns -2011 'Order was not canceled due to cancel restrictions.'

> "Unknown order sent."                                           | The order (by either `orderId`, `clOrdId`, `origClOrdId`) could not be found.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 5. Error -2011 on cancelRestrictions

The exact error when a cancelRestrictions condition fails. errors.md describes the case as: cancelRestrictions was ONLY_NEW but the status was not NEW, or ONLY_PARTIALLY_FILLED but the status was not PARTIALLY_FILLED.

> {
>     "code": -2011,
>     "msg": "Order was not canceled due to cancel restrictions."
> }

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#regarding-cancelrestrictions>

### 5. -2011 code text

errors.md lists -2011 only by its code name. The specific reason is in msg, from the matching-engine message table shared by -1010, -2010, -2011 and -2038.

> ### -2011 CANCEL_REJECTED
>  * CANCEL_REJECTED

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 6. GET /api/v3/openOrders

symbol is optional. Weight is 6 for one symbol and 80 with no symbol, in which case orders for all symbols come back in one array. Data source: Memory => Database. The order objects use the query-order field layout.

> **Weight:**
> 6 for a single symbol; **80** when the symbol parameter is omitted

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#current-open-orders-user_data>

### 6. GET /api/v3/myTrades - params

symbol is REQUIRED. Optional: orderId, startTime, endTime, fromId and limit (default 500, max 1000). Weight is 20, or 5 with orderId. The startTime-to-endTime span cannot exceed 24 hours. With fromId, trades with id >= fromId are returned.

> symbol | STRING | YES |
> orderId|LONG|NO| This can only be used in combination with `symbol`.
> startTime | LONG | NO |
> endTime | LONG | NO |
> fromId | LONG | NO | TradeId to fetch from. Default gets most recent trades.
> limit | INT | NO | Default: 500; Maximum: 1000.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-trade-list-user_data>

Note: Also verbatim: '* If `fromId` is set, it will get trades >= that `fromId`.' and weights '|Without orderId|20|' '|With orderId|5|'

### 6. GET /api/v3/myTrades - allowed combinations

Only the listed parameter combinations are supported, and the time window cannot exceed 24 hours.

> * The time between `startTime` and `endTime` can't be longer than 24 hours.
> * These are the supported combinations of all parameters:
>   * `symbol`
>   * `symbol` + `orderId`
>   * `symbol` + `startTime`
>   * `symbol` + `endTime`
>   * `symbol` + `fromId`
>   * `symbol` + `startTime` + `endTime`
>   * `symbol`+ `orderId` + `fromId`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-trade-list-user_data>

### 6. GET /api/v3/myTrades - fields

Each trade has symbol, id, orderId, orderListId, price, qty, quoteQty, commission, commissionAsset, time, isBuyer, isMaker and isBestMatch.

> "symbol": "BNBBTC",
>         "id": 28457,
>         "orderId": 100234,
>         "orderListId": -1,
>         "price": "4.00000100",
>         "qty": "12.00000000",
>         "quoteQty": "48.000012",
>         "commission": "10.10000000",
>         "commissionAsset": "BNB",
>         "time": 1499865549590,
>         "isBuyer": true,
>         "isMaker": false,
>         "isBestMatch": true

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-trade-list-user_data>

### 6. GET /api/v3/account

Weight 20. Optional omitZeroBalances (default false). The response includes canTrade, canWithdraw, canDeposit, brokered, requireSelfTradePrevention, preventSor, updateTime, accountType, balances, permissions (e.g. ["SPOT"]) and uid. balances is a list of {asset, free, locked}, with values as strings.

> "canTrade": true,
>     "canWithdraw": true,
>     "canDeposit": true,
>     "brokered": false,
>     "requireSelfTradePrevention": false,
>     "preventSor": false,
>     "updateTime": 123456789,
>     "accountType": "SPOT",
>     "balances": [
>         {
>             "asset": "BTC",
>             "free": "4723846.89208129",
>             "locked": "0.00000000"
>         },

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-information-user_data>

### 7. Order status values

Documented order status meanings. PENDING_CANCEL is 'Currently unused'. PENDING_NEW also exists, for order lists.

> `NEW` | The order has been accepted by the engine.
> `PENDING_NEW`|The order is in a pending phase until the working order of an order list has been fully filled.
> `PARTIALLY_FILLED`| A part of the order has been filled.
> `FILLED` | The order has been completed.
> `CANCELED` | The order has been canceled by the user.
> `PENDING_CANCEL` | Currently unused
> `REJECTED`       | The order was not accepted by the engine and not processed.
> `EXPIRED` | The order was canceled according to the order type's rules (e.g. LIMIT FOK orders with no fill, LIMIT IOC or MARKET orders that partially fill) <br/> or by the exchange, (e.g. orders canceled during liquidation, orders canceled during maintenance)
> `EXPIRED_IN_MATCH` | The order was expired by the exchange due to STP. (e.g. an order with `EXPIRE_TAKER` will match with existing orders on the book with the same account or same `tradeGroupId`)

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#order-status-status>

### 7. timeInForce values

timeInForce takes GTC, IOC or FOK.

> `GTC` | Good Til Canceled <br/> An order will be on the book unless the order is canceled.
> `IOC` | Immediate Or Cancel <br/> An order will try to fill the order as much as it can before the order expires.
> `FOK`| Fill or Kill <br/> An order will expire if the full order cannot be filled upon execution.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#timeinforce>

### 8. HTTP 4XX vs 5XX

4XX means a malformed request (the sender's fault). 5XX means an internal error; the execution status is UNKNOWN and must not be treated as a failure. 403 means a WAF rule was violated. 409 means a cancelReplace partly succeeded.

> * HTTP `4XX` return codes are used for malformed requests; the issue is on the sender's side.
> ...
> * HTTP `5XX` return codes are used for internal errors; the issue is on Binance's side.
>   It is important to **NOT** treat this as a failure operation; the execution status is
>   **UNKNOWN** and could have been a success.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#http-return-codes>

Note: Also verbatim: '* HTTP `403` return code is used when a WAF (Web Application Firewall) rule has been violated.' and '* HTTP `409` return code is used when a cancelReplace order partially succeeds.'

### 8. 10s timeout / -1007

The API times out after 10 seconds with -1007, and the execution status is unknown. If the result has not appeared in the User Data Stream, query the order's status.

> * APIs have a timeout of 10 seconds when processing a request. If a response from the Matching Engine takes longer than this, the API responds with "Timeout waiting for response from backend server. Send status unknown; execution status unknown." [(-1007 TIMEOUT)](errors.md#-1007-timeout)
>   * This does not always mean that the request failed in the Matching Engine.
>   * If the status of the request has not appeared in [User Data Stream](user-data-stream.md), please perform an API query for its status.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#general-api-information>

### 8. -1006 unknown execution

-1006 UNEXPECTED_RESP is another documented 'execution status unknown' error.

> ### -1006 UNEXPECTED_RESP
>  * An unexpected response was received from the message bus. Execution status unknown.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 8. 429 / 418 / Retry-After

Repeated 429s lead to a 418 IP ban lasting 2 minutes to 3 days. Retry-After gives the wait in seconds. Limits are per IP, not per API key.

> * **Repeatedly violating rate limits and/or failing to back off after receiving 429s will result in an automated IP ban (HTTP status 418).**
> * IP bans are tracked and **scale in duration** for repeat offenders, **from 2 minutes to 3 days**.
> * A `Retry-After` header is sent with a 418 or 429 responses and will give the **number of seconds** required to wait, in the case of a 429, to prevent a ban, or, in the case of a 418, until the ban is over.
> * **The limits on the API are based on the IPs, not the API keys.**

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#ip-limits>

### 8. -1003 too many requests (missed by extractor)

-1003 TOO_MANY_REQUESTS is the error for exceeding request-weight limits, including the 'IP banned until' message.

> ### -1003 TOO_MANY_REQUESTS
>  * Too many requests queued.
>  * Too much request weight used; current limit is %s request weight per %s. Please use WebSocket Streams for live updates to avoid polling the API.
>  * Way too much request weight used; IP banned until %s. Please use WebSocket Streams for live updates to avoid bans.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 8. Used-weight header

Every response carries an X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter) header with the IP's current used weight.

> * Every request will contain `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` in the response headers which has the current used weight for the IP for all request rate limiters defined.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#ip-limits>

### 8. Order rate limits (ORDERS / unfilled order count)

Successful orders return X-MBX-ORDER-COUNT-* headers; rejected orders may not. Exceeding the limit returns 429 with Retry-After. The unfilled order count is tracked per account. Read the actual limits from exchangeInfo rateLimits.

> * Every successful order response will contain a `X-MBX-ORDER-COUNT-(intervalNum)(intervalLetter)` header indicating how many orders you have placed for that interval. <br></br> To monitor this, refer to [`GET api/v3/rateLimit/order`](#query-unfilled-order-count).
> * Rejected/unsuccessful orders are not guaranteed to have `X-MBX-ORDER-COUNT-**` headers in the response.
> * If you have exceeded this, you will receive a 429 error with the `Retry-After` header.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#unfilled-order-count>

Note: Per-account, verbatim: '* **The number of unfilled orders is tracked for each account.**'

### 8. ORDERS rate limiter enum example

enums.md shows a sample ORDERS rate limiter object. It is only a sample; read live values from /api/v3/exchangeInfo.

> {
>     "rateLimitType": "ORDERS",
>     "interval": "SECOND",
>     "intervalNum": 1,
>     "limit": 10
> }

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/enums.md#rate-limiters-ratelimittype>

### 8. -1015 too many orders

-1015 TOO_MANY_ORDERS is the error for exceeding the order rate limit.

> ### -1015 TOO_MANY_ORDERS
>  * Too many new orders.
>  * Too many new orders; current limit is %s orders per %s.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 8. -1013 filter failure

-1013 INVALID_MESSAGE means the API rejected the request before it reached the Matching Engine. The message is a filter-failure string or an order-placement failure message.

> ### -1013 INVALID_MESSAGE
>   * The request is rejected by the API. (i.e. The request didn't reach the Matching Engine.)
>   * Potential error messages can be found in [Filter Failures](#filter-failures) or [Failures during order placement](#other-errors).

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 8. Filter failure messages

Exact filter-failure message strings for the filters that matter for order placement.

> "Filter failure: PRICE_FILTER" | `price` is too high, too low, and/or not following the tick size rule for the symbol.
> "Filter failure: PERCENT_PRICE" | `price` is too high or too low from the average of previous trade prices. Details [here](./filters.md#percent_price).
> "Filter failure: PERCENT_PRICE_BY_SIDE" | `price` is too high or too low from the average of previous trade prices. Details [here](./filters.md#percent_price_by_side).
> "Filter failure: LOT_SIZE" | `quantity` is too high, too low, and/or not following the step size rule for the symbol.
> "Filter failure: MIN_NOTIONAL" | `price` * `quantity` is too low to be a valid order for the symbol.
> "Filter failure: NOTIONAL"    | `price` * `quantity` is not within range of the `minNotional` and `maxNotional`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md#filter-failures>

### 8. -2010 new order rejected / insufficient balance

-2010 NEW_ORDER_REJECTED carries a matching-engine message. The insufficient-balance message is 'Account has insufficient balance for requested action.'

> ### -2010 NEW_ORDER_REJECTED
>  * NEW_ORDER_REJECTED
> ...
> "Account has insufficient balance for requested action."        | Not enough funds to complete the action.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 8. -2010 other relevant messages

Other documented matching-engine messages that matter for this adapter.

> "Market is closed."                                             | The symbol is not trading.
> ...
> "Stop loss limit orders are not supported for this symbol."     | `STOP_LOSS_LIMIT` is not enabled on the symbol.
> ...
> "Order would trigger immediately."                              | The order's stop price is not valid when compared to the last traded price.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

### 8. -2015 invalid key

-2015 REJECTED_MBX_KEY covers an invalid API key, IP or permission. -2014 BAD_API_KEY_FMT means the API key format is invalid.

> ### -2015 REJECTED_MBX_KEY
>  * Invalid API-key, IP, or permissions for action.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/errors.md>

Note: -2014, verbatim: '### -2014 BAD_API_KEY_FMT\n * API-key format invalid.'

### 9. PRICE_FILTER

PRICE_FILTER applies to price and stopPrice: price >= minPrice, price <= maxPrice, price % tickSize == 0. Setting any of those values to 0 disables that rule.

> Any of the above variables can be set to 0, which disables that rule in the `price filter`. In order to pass the `price filter`, the following must be true for `price`/`stopPrice` of the enabled rules:
> 
> * `price` >= `minPrice`
> * `price` <= `maxPrice`
> * `price` % `tickSize` == 0

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#price_filter>

### 9. LOT_SIZE

LOT_SIZE applies to quantity and icebergQty: quantity >= minQty, quantity <= maxQty, quantity % stepSize == 0. Unlike PRICE_FILTER, the LOT_SIZE section does not say that 0 disables a rule.

> * `quantity` >= `minQty`
> * `quantity` <= `maxQty`
> * `quantity` % `stepSize` == 0

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#lot_size>

### 9. NOTIONAL

NOTIONAL requires minNotional <= price * quantity <= maxNotional. applyMinToMarket and applyMaxToMarket apply only to MARKET orders. A LIMIT order is therefore checked against both bounds; that is an inference, since no sentence names LIMIT. For MARKET orders the price used is the reference price, else the VWAP over avgPriceMins, else the last price when avgPriceMins is 0.

> * `applyMinToMarket` determines whether `minNotional` will be applied to `MARKET` orders.
> * `applyMaxToMarket` determines whether `maxNotional` will be applied to `MARKET` orders.
> ...
> An order will pass this filter evaluation if:
> * `price` * `quantity` <= `maxNotional`
> * `price` * `quantity` >= `minNotional`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#notional>

### 9. MIN_NOTIONAL (legacy)

MIN_NOTIONAL requires price * quantity >= minNotional. applyToMarket controls whether it also applies to MARKET orders.

> An order will pass this filter evaluation if:
> * `price` * `quantity` >= `minNotional`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#min_notional>

### 9. PERCENT_PRICE_BY_SIDE

PERCENT_PRICE_BY_SIDE bounds a BUY price with bidMultiplierUp/Down and a SELL price with askMultiplierUp/Down, applied to the reference average price.

> A `BUY` order will pass this filter evaluation if:
> * `price` <= `average of previous trade prices` * `bidMultiplierUp`
> * `price` >= `average of previous trade prices` * `bidMultiplierDown`
> 
> A `SELL` order will pass this filter evaluation if:
> * `price` <= `average of previous trade prices` * `askMultiplierUp`
> * `price` >= `average of previous trade prices` * `askMultiplierDown`

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#percent_price_by_side>

### 9. PERCENT_PRICE_BY_SIDE reference price

The reference price is the symbol's non-null reference price if one exists. Otherwise it is the VWAP over avgPriceMins, or the last price when avgPriceMins is 0.

> * When a non-null [reference price](./faqs/price_range_execution_rules.md) for the symbol exists, it is used in the filter evaluation.
> * When a non-null reference price for the symbol does not exist, then the volume weighted average price over the preceding `avgPriceMins` minutes is used in the filter evaluation.
>   * If `avgPriceMins` is 0, then the last price is used in the filter evaluation.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/filters.md#percent_price_by_side>

### 9. Filters are optional per symbol

In exchangeInfo every filter is optional; a symbol can define any subset.

> "filters": [
>                 // These are defined in the Filters section.
>                 // All filters are optional
>             ],

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#exchange-information>

### 10. Testnet key creation / login

Testnet API keys are created on testnet.binance.vision after logging in with a GitHub account.

> * API keys can be created on the [SPOT Test Network](https://testnet.binance.vision) upon logging in with your Github account.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md>

### 10. Testnet supported key types

Testnet supports HMAC, RSA and Ed25519 keys. For RSA and Ed25519 you generate the keypair, register the public key, and receive an API key.

> * We support HMAC, RSA, and Ed25519 keys. For more information, please see [API Key types](../faqs/api_key_types.md).

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/rest-api.md>

Note: testnet/general-info.md, verbatim: 'Like RSA API keys, Ed25519 keys are *asymmetric*: you generate a keypair, share the public key with Binance, and use your private key to sign requests.'

### 10. Testnet Ed25519 key generation

Generate the Ed25519 keypair locally with openssl, then register the public key on the Spot Test Network.

> openssl genpkey -algorithm ed25519 -out test-prv-key.pem
> ...
> Step 3: Register your public key on the Spot Test Network.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

### 10. Testnet balances (virtual)

Every new testnet user automatically receives balances in many assets. The funds are virtual and cannot be transferred in or out.

> All users registering on the Spot Test Network automatically receive a balance in many different assets. Please note that these are not real assets and can be used only on the Spot Test Network itself.
> 
> All funds on the Spot Test Network are virtual, and can not be transferred in/out of the Spot Test Network.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

### 10. Testnet reset schedule

Testnet resets about once a month with no prior notice. All pending and executed orders are deleted and balances are refilled. API keys survive resets (since August 2020). The testnet CHANGELOG records 'Data reset' entries on 2026-10-07 and 2026-09-09, and has a 2026-08-05 section.

> The Spot Test Network is periodically reset to a blank state. That includes all pending and executed orders. During that reset procedure, all users automatically receive a fresh allowance of all assets.
> 
> These resets happen approximately **once per month**, and we do not offer prior notification for them.
> 
> Starting from August 2020, API Keys are preserved during resets. Users no longer need to re-register new API Keys after a reset.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

Note: I confirmed the reset text under the 2026-10-07 and 2026-09-09 headings. The 2026-08-05 heading exists, but I did not print its contents.

### 10. Testnet rate limits / filters vs production

Testnet limits and filters are 'generally the same' as production; query exchangeInfo for current values. Documented differences: no /sapi; klines and uiKlines always return the same data; testnet/enums.md lists only SPOT under permissions.

> **IP Limits**, **Order Rate Limits**, **Exchange Filters** and **Symbol Filters** on the Spot Test Network are generally the same as on the Spot API.
> 
> All users are encouraged to regularly query the API to get the most up-to-date rate limits & filters, for example by doing:
> 
> ```
> curl "https://testnet.binance.vision/api/v3/exchangeInfo"
> ```

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md>

Note: Verbatim: 'On the Spot Test Network, these 2 requests always return the same data.' (klines/uiKlines). testnet/enums.md, under 'Account and Symbol Permissions (permissions)', lists only `SPOT`.

### 10. Testnet not synced with live

Testnet features may differ from the live exchange.

> **Note:** All features here will only apply to the [SPOT Testnet](https://testnet.binance.vision/).
> This is not always synced with the live exchange.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/testnet/CHANGELOG.md>

### 11. STP default mode

Each symbol sets defaultSelfTradePreventionMode and allowedSelfTradePreventionModes in exchangeInfo, so there is no global default; read it per symbol. The documented examples show the default as NONE. Sending a mode the symbol does not allow returns -1013 'This symbol does not allow the specified self-trade prevention mode.'

> Symbols may be configured to allow different sets of STP modes and take different default STP modes.
> 
> `defaultSelfTradePreventionMode` - Orders will use this STP mode if the user does not provide one on order placement.
> 
> `allowedSelfTradePreventionModes` - Defines the allowed set of STP modes for order placement on that symbol.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/stp_faq.md>

### 11. STP disallowed-mode error

The exact error for an STP mode the symbol does not allow.

> "code": -1013,
>     "msg": "This symbol does not allow the specified self-trade prevention mode."

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/stp_faq.md>

### 11. STP status

An order expired by STP has status EXPIRED_IN_MATCH.

> ### How do I know if an order expired due to STP?
> 
> The order will have the status `EXPIRED_IN_MATCH`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/stp_faq.md>

### 11. STP prevented quantity equation

Quantity available = origQty - executedQty - preventedQuantity. When it reaches 0, the order leaves the book with status EXPIRED_IN_MATCH, FILLED or EXPIRED.

> original order quantity - executed quantity - prevented quantity = quantity available for further execution
> ```
> 
> When an order's available quantity goes to zero, the order will be removed from the order book and the status will be one of `EXPIRED_IN_MATCH`, `FILLED`, or `EXPIRED`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/stp_faq.md>

### 12. Key permissions - GET /sapi/v1/account/apiRestrictions

A signed USER_DATA endpoint on the production base (not on the Spot Test Network, which has no `/sapi`, §1). Weight 1 (IP); `timestamp` mandatory, `recvWindow` optional. It reports the calling key's own permissions. Withdrawals can only be enabled on an IP-restricted key.

> GET `/sapi/v1/account/apiRestrictions`
>
> ```
> {
>     "ipRestrict": false,
>     "createTime": 1698645219000,
>     "enableReading": true,
>     "enableWithdrawals": false,  // This option allows you to withdraw via API. You must apply the IP Access Restriction filter in order to enable withdrawals
>     "enableInternalTransfer": false,  // This option authorizes this key to transfer funds between your master account and your sub account instantly
>     "enableMargin": false,  // This option can be adjusted after the Cross Margin account transfer is completed
>     "enableFutures": false,
>     "permitsUniversalTransfer": false,  // Authorizes this key to be used for a dedicated universal transfer API to transfer multiple supported currencies.
>     "enableVanillaOptions": false,  // Authorizes this key to Vanilla options trading
>     "enableFixApiTrade": false,
>     "enableFixReadOnly": true,
>     "enableSpotAndMarginTrading": false, // Spot and margin trading
>     "enablePortfolioMarginTrading": true
> }
> ```

Source: <https://developers.binance.com/legacy-docs/wallet/account/api-key-permission> (read 2026-10-30; the current page at `developers.binance.com/docs/wallet/account/api-key-permission` lists the same thirteen fields). The generated `binance-sdk` model `GetApiKeyPermissionResponse` (docs.rs) has the same thirteen fields, each optional: the judge treats a missing one as a failure.

### 12. Account permissions - GET /api/v3/account

`canTrade`, `canWithdraw`, `canDeposit`, `accountType` and `permissions` describe the *account*, not the key; they cannot replace the key check. Weight 20; `omitZeroBalances` optional.

> ```javascript
>     "canTrade": true,
>     "canWithdraw": true,
>     "canDeposit": true,
>     ...
>     "accountType": "SPOT",
>     ...
>     "permissions": [
>         "SPOT"
>     ],
> ```

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#account-information-user_data>

### 12. Best bid and ask - GET /api/v3/ticker/bookTicker

The venue's best bid and ask from memory, weight 2 for one symbol. The response has **no timestamp**: its time is when the answer arrives (ADR-034's live-quote path, S40b).

> ```javascript
> {
>     "symbol": "LTCBTC",
>     "bidPrice": "4.00000000", // best bid price.
>     "bidQty": "431.00000000", // bid/ask qty.
>     "askPrice": "4.00000200", // best ask price.
>     "askQty": "9.00000000"    // bid/ask qty.
> }
> ```

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#symbol-order-book-ticker> ("**Data Source:** Memory")

### 12. Key types and IP restriction (Binance FAQ)

System-generated (HMAC) keys without an IP restriction can only read; self-generated Ed25519 or RSA keys can be given more without one. Withdrawals require an IP restriction. Binance strongly recommends a restriction for anything beyond reading. This system is stricter: it requires the restriction for any trading key (ADR-034, B-11).

> Unrestricted-IP-Access HMAC API Key(s) won't have any permission other than reading.
>
> Adding IP access (IPv4 format) restrictions is mandatory to enable withdrawal permission.

Source: <https://www.binance.com/en/support/faq/how-to-create-api-keys-on-binance-360002502072> ("Last updated: 20 March 2025", read 2026-10-30)

## Corrected by the checker

### 2. Signing - Ed25519 payload and encoding

Ed25519 steps: (1) build the payload as '&'-joined parameter=value pairs, percent-encoded (query string concatenated with the body); (2) sign it with the Ed25519 private key and base64-encode the output; (3) percent-encode the base64 string. The payload and signature are case-sensitive. Two corrections to the extractor's caveat. First, the Ed25519 openssl example is 'openssl dgst -keyform PEM -sha256 -sign ./test-prv-key.pem | openssl enc -base64'. The '-sha256 -sign' does look carried over from the RSA example, and the sample signatures are 344 base64 characters long, which is RSA length; a real Ed25519 signature is 64 bytes, or 88 base64 characters. Do not use those vectors as Ed25519 test vectors. Second, the Python example in rest-api.md builds the payload with urllib.parse.urlencode(params, encoding='UTF-8'), not a raw '&'.join. The raw '&'.join form appears only in the testnet/general-info.md example. Both then call base64.b64encode(private_key.sign(payload.encode('ASCII'))) and send the request as form data, and requests percent-encodes the signature during form encoding.

> **Step 2: Compute the signature**
> 
> 1. Sign the payload.
> 2. Encode the output as a base64 string.
> 
> Note that the payload and the resulting `signature` are **case-sensitive**.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#ed25519-keys>

Note: rest-api.md Ed25519 Python example, verbatim: "payload = urllib.parse.urlencode(params, encoding='UTF-8')\nsignature = base64.b64encode(private_key.sign(payload.encode('ASCII')))". The 344-character length is an observation from the example output, not a documented statement.

### 11. STP can expire own orders (EXPIRED_IN_MATCH)

The FAQ documents five modes, not just the four NONE/EXPIRE_* modes the extractor listed: NONE, EXPIRE_TAKER, EXPIRE_MAKER, EXPIRE_BOTH, DECREMENT and TRANSFER. Under NONE the self-trade happens. The EXPIRE_* modes expire the taker's remaining quantity, the maker's, or both. DECREMENT raises the prevented quantity of both orders; the smaller order expires, or both if equal. TRANSFER acts like DECREMENT within one account. An expired order gets status EXPIRED_IN_MATCH and preventedQuantity. The taker's mode normally decides the outcome. Exception: TRANSFER applies only if both orders specify TRANSFER; a TRANSFER taker against any other maker mode behaves as DECREMENT.

> `DECREMENT`  - This mode increases the `prevented quantity` of *both* orders by the amount of the prevented match. The smaller of the two orders will expire, or both if they have the same quantity.
> 
> `TRANSFER` - If orders are from the same account, then the behavior is the same as `DECREMENT`.
> If orders are from different accounts with the same `tradeGroupId`, then in addition to the behavior of `DECREMENT`, the `last prevented quantity` and its notional are transferred between the two accounts.
> 
> STP behavior is typically determined by the STP mode of the **taker order** only. The exception is that for STP `TRANSFER` to occur, both the maker and taker orders must specify STP mode `TRANSFER`. If the taker order specifies STP mode `TRANSFER`, but the maker order specifies a different STP mode, then the STP behavior is `DECREMENT`.

Source: <https://github.com/binance/binance-spot-api-docs/blob/master/faqs/stp_faq.md>

Note: The FAQ says 'five possible modes' but then lists six (NONE, EXPIRE_TAKER, EXPIRE_MAKER, EXPIRE_BOTH, DECREMENT, TRANSFER). The extractor's quote of the NONE and EXPIRE_* lines is verbatim.
