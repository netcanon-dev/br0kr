# _pgconn.sh — sourced by backup_ledger.sh and restore_ledger.sh; not run on its own (review OPS-05).
#
# pgconn_split <conninfo>
#   Moves a password out of a libpq connection string, so it never reaches a process's argv: argv is
#   readable by every local user through `ps` and /proc/<pid>/cmdline, while a process's environment is
#   readable only by its own user and root. On success it sets:
#     PGCONN_SAFE  the connection string without its password
#     PGPASSWORD   exported with that password, when the string carried one. It replaces an inherited
#                  PGPASSWORD, because libpq itself lets the string's password win.
#   Understands both libpq forms: keyword/value (`password=x`, `password = 'a b\'c'`, with libpq's
#   backslash escapes) and URI (`postgresql://user:pass@host/db`, percent-decoded). Returns 1, with a
#   message on stderr, when a password is still visible after stripping (for example `?password=` in a URI
#   query, or a second `password=`), rather than pass it on. Uses only bash builtins, so the password is
#   never in an argv here either.

pgconn_split() {
  local dsn="$1" pw="" have_pw=0 q="'"
  # keyword/value: a quoted value '...' or an unquoted run to the next whitespace, either one with \x escapes
  # (libpq's grammar). An unquoted value may itself contain a quote (`password=ab'cd` is the password ab'cd),
  # so the unquoted class excludes only whitespace and backslash; leftmost-longest matching still takes the
  # quoted form for a value like 'a b'.
  local re_kv="(^|[[:space:]])password[[:space:]]*=[[:space:]]*(${q}([^${q}\\\\]|\\\\.)*${q}|([^[:space:]\\\\]|\\\\.)*)"
  local re_uri='^(postgres(ql)?://)([^:@/?]*):([^@/?]*)@'
  local re_left='(^|[[:space:]?&])password[[:space:]]*='

  if [[ $dsn =~ $re_uri ]]; then
    local rest="${dsn:${#BASH_REMATCH[0]}}" enc="${BASH_REMATCH[4]}"
    if [[ ${enc//%[0-9A-Fa-f][0-9A-Fa-f]/} == *%* ]]; then   # libpq rejects a malformed %xx too
      echo "the connection URI's password has malformed percent-encoding" >&2
      return 1
    fi
    dsn="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${rest}"
    enc="${enc//\\/\\\\}"                    # keep a raw backslash literal through the %b below
    printf -v pw '%b' "${enc//%/\\x}"        # percent-decode (%40 -> @)
    have_pw=1
  elif [[ $dsn =~ $re_kv ]]; then
    local whole="${BASH_REMATCH[0]}" lead="${BASH_REMATCH[1]}" val="${BASH_REMATCH[2]}"
    if [[ ${#val} -ge 2 && $val == "$q"* && $val == *"$q" ]]; then
      val="${val:1:${#val}-2}"               # a quoted value: drop the surrounding quotes
    fi
    local i c out=""
    for ((i = 0; i < ${#val}; i++)); do      # undo \' and \\ (any \x means x)
      c="${val:i:1}"
      if [[ $c == "\\" ]]; then i=$((i + 1)); c="${val:i:1}"; fi
      out+="$c"
    done
    pw="$out"
    have_pw=1
    dsn="${dsn/"$whole"/$lead}"
  fi

  if [[ $dsn =~ $re_left ]]; then
    echo "the connection string still carries a password this script cannot move out of argv" \
         "(a URI ?password= or a second password=): pass it in PGPASSWORD or ~/.pgpass instead" >&2
    return 1
  fi
  PGCONN_SAFE="$dsn"
  if [ "$have_pw" = 1 ]; then
    export PGPASSWORD="$pw"
  fi
  return 0
}
