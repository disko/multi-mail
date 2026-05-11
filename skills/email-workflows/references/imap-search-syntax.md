# IMAP SEARCH Syntax Reference

The `email_search_messages` tool accepts IMAP SEARCH criteria as defined in RFC 3501.
Multiple criteria are ANDed together by default. Use the OR keyword for disjunction.

## Simple Criteria

| Criteria | Meaning |
|----------|---------|
| `ALL` | All messages |
| `ANSWERED` | Messages with \Answered flag |
| `DELETED` | Messages with \Deleted flag |
| `DRAFT` | Messages with \Draft flag |
| `FLAGGED` | Messages with \Flagged flag |
| `NEW` | Recent + unseen |
| `OLD` | Not recent |
| `RECENT` | Messages with \Recent flag |
| `SEEN` | Messages with \Seen flag |
| `UNANSWERED` | Not answered |
| `UNDELETED` | Not deleted |
| `UNDRAFT` | Not draft |
| `UNFLAGGED` | Not flagged |
| `UNSEEN` | Not seen |

## String Criteria

| Criteria | Meaning |
|----------|---------|
| `BCC "string"` | BCC contains string |
| `BODY "string"` | Body contains string |
| `CC "string"` | CC contains string |
| `FROM "string"` | From contains string |
| `SUBJECT "string"` | Subject contains string |
| `TEXT "string"` | Header or body contains string |
| `TO "string"` | To contains string |

## Date Criteria

Dates must use `DD-Mon-YYYY` format, e.g. `01-Jan-2026`.

| Criteria | Meaning |
|----------|---------|
| `BEFORE date` | Internal date is before |
| `ON date` | Internal date is on |
| `SINCE date` | Internal date is on or after |
| `SENTBEFORE date` | Date header is before |
| `SENTON date` | Date header is on |
| `SENTSINCE date` | Date header is on or after |

## Size Criteria

| Criteria | Meaning |
|----------|---------|
| `LARGER n` | Size > n bytes |
| `SMALLER n` | Size < n bytes |

## UID Criteria

| Criteria | Meaning |
|----------|---------|
| `UID uid-set` | Messages with given UIDs (e.g. `UID 1:100`) |

## Combining Criteria

**AND** (implicit — just list criteria):
```
FROM "alice" SINCE 01-Jan-2026 UNSEEN
```

**OR** (explicit keyword, takes exactly two operands):
```
OR FROM "alice" FROM "bob"
```

**NOT**:
```
NOT FROM "spam@example.com"
```

**Nested OR** (for more than two):
```
OR FROM "alice" OR FROM "bob" FROM "carol"
```

## Examples

Find unread messages from alice since March 2026:
```
FROM "alice@example.com" SINCE 01-Mar-2026 UNSEEN
```

Find messages with "invoice" in subject or body:
```
OR SUBJECT "invoice" BODY "invoice"
```

Find large messages:
```
LARGER 5000000
```

Find flagged unread messages:
```
FLAGGED UNSEEN
```
