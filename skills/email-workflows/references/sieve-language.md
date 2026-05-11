# Sieve Language Reference

Sieve (RFC 5228) is a server-side mail filtering language. Scripts are uploaded
via ManageSieve and executed by the mail server on incoming messages.

## Structure

A Sieve script consists of:
1. `require` statements (declare extensions used)
2. Control structures (`if`, `elsif`, `else`, `stop`)
3. Actions (`keep`, `discard`, `redirect`, `fileinto`, `reject`, etc.)

## Require

Extensions must be declared before use:

```sieve
require ["fileinto", "reject", "vacation", "copy", "imap4flags", "regex"];
```

Common extensions:
| Extension | Purpose |
|-----------|---------|
| `fileinto` | Move messages to a folder |
| `reject` | Reject with a message |
| `vacation` | Auto-reply |
| `copy` | Keep a copy when redirecting |
| `imap4flags` | Set IMAP flags (\\Seen, \\Flagged, etc.) |
| `regex` | Regular expression matching |
| `body` | Match against message body |
| `envelope` | Match envelope (MAIL FROM / RCPT TO) |
| `relational` | Numeric comparisons (:count, :value) |
| `comparator-i;ascii-numeric` | Numeric string comparison |
| `editheader` | Add or delete headers |
| `variables` | Variable storage and substitution |
| `include` | Include other scripts |
| `notify` | Send notifications |
| `duplicate` | Duplicate message detection |

## Tests

### Address test

Match against structured address headers (From, To, Cc, etc.):

```sieve
if address :is "from" "boss@example.com" { ... }
if address :domain "from" "example.com" { ... }
if address :localpart "to" "admin" { ... }
if address :contains "from" "noreply" { ... }
```

Parts: `:localpart`, `:domain`, `:all` (default)

### Header test

Match any header value:

```sieve
if header :contains "subject" "urgent" { ... }
if header :is "X-Spam-Status" "Yes" { ... }
if header :matches "subject" "*invoice*" { ... }
```

### Envelope test (requires `envelope`)

Match SMTP envelope:

```sieve
require ["envelope"];
if envelope :is "from" "mailer-daemon@example.com" { ... }
```

### Size test

Match message size:

```sieve
if size :over 10M { ... }
if size :under 1K { ... }
```

Units: K (kilobytes), M (megabytes), G (gigabytes)

### Exists test

Check if a header exists:

```sieve
if exists "X-Priority" { ... }
```

### Body test (requires `body`)

```sieve
require ["body"];
if body :contains "unsubscribe" { ... }
```

## Match types

| Type | Meaning |
|------|---------|
| `:is` | Exact match |
| `:contains` | Substring match |
| `:matches` | Glob-style (`*` = any, `?` = single char) |
| `:regex` | Regular expression (requires `regex` extension) |

## Comparators

| Comparator | Meaning |
|------------|---------|
| `i;ascii-casemap` | Case-insensitive (default) |
| `i;octet` | Case-sensitive byte comparison |

```sieve
if header :comparator "i;octet" :is "X-Token" "aBcD" { ... }
```

## Actions

### keep (default)

Deliver to inbox (implicit if no other action matches):

```sieve
keep;
```

### fileinto (requires `fileinto`)

Deliver to a specific folder:

```sieve
require ["fileinto"];
fileinto "Archive";
fileinto "INBOX.Lists.Linux";
```

### discard

Silently delete the message:

```sieve
discard;
```

### redirect

Forward to another address:

```sieve
redirect "other@example.com";
```

With a copy kept locally (requires `copy`):

```sieve
require ["copy"];
redirect :copy "backup@example.com";
```

### reject / ereject (requires `reject`)

Reject with a message to the sender:

```sieve
require ["reject"];
reject "I don't accept mail from this address.";
```

### vacation (requires `vacation`)

Auto-reply:

```sieve
require ["vacation"];
vacation :days 7
  :subject "Out of Office"
  :from "me@example.com"
  "I am currently out of the office. I will reply when I return.";
```

Parameters:
- `:days N` — minimum days between replies to same sender
- `:subject "text"` — reply subject
- `:from "addr"` — reply from address
- `:addresses ["a@x", "b@x"]` — additional addresses that are "me"

### flags (requires `imap4flags`)

Set IMAP flags:

```sieve
require ["imap4flags"];
setflag "\\Flagged";
addflag "\\Seen";
```

Can combine with fileinto:

```sieve
require ["fileinto", "imap4flags"];
fileinto :flags "\\Seen" "Notifications";
```

## Control flow

```sieve
if TEST {
  ACTION;
} elsif TEST {
  ACTION;
} else {
  ACTION;
}
```

### Boolean operators

```sieve
if allof (TEST1, TEST2) { ... }   # AND
if anyof (TEST1, TEST2) { ... }   # OR
if not TEST { ... }                # NOT
```

### stop

Stop processing (implicit keep does NOT happen after stop unless explicit):

```sieve
fileinto "Archive";
stop;
```

## Variables (requires `variables`)

```sieve
require ["variables"];
set "name" "value";
if header :matches "subject" "Re: *" {
  set "original_subject" "${1}";
}
```

## Complete examples

### Sort mailing lists

```sieve
require ["fileinto"];
if header :contains "list-id" "dev.lists.example.com" {
  fileinto "Lists.Dev";
} elsif header :contains "list-id" "announce.lists.example.com" {
  fileinto "Lists.Announce";
}
```

### Spam filtering with flag

```sieve
require ["fileinto", "imap4flags"];
if header :is "X-Spam-Status" "Yes" {
  fileinto :flags "\\Seen" "Junk";
  stop;
}
```

### Priority handling

```sieve
require ["fileinto", "imap4flags"];
if address :is "from" "boss@example.com" {
  addflag "\\Flagged";
} elsif size :over 5M {
  fileinto "Large";
}
```

### Multi-rule script

```sieve
require ["fileinto", "reject", "vacation", "imap4flags"];

# Reject known spam
if address :is "from" "spammer@example.com" {
  discard;
  stop;
}

# Auto-file GitHub notifications
if address :is "from" "notifications@github.com" {
  fileinto "GitHub";
  stop;
}

# Flag messages from boss
if address :is "from" "boss@example.com" {
  addflag "\\Flagged";
  keep;
  stop;
}

# Vacation auto-reply
vacation :days 7
  :subject "Away"
  "I'm on holiday until next week.";
```
