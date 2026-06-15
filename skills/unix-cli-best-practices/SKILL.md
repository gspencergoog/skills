---
name: unix-cli-best-practices
description: Pro-level command line patterns for working with standard macOS/BSD Unix tools (grep, find, sed, awk) and their modern alternatives (ripgrep, fd). Use this skill when executing shell commands for fast searching, text processing, or codebase navigation.
---

# Unix CLI Best Practices

This guide provides efficient, safe, and portable techniques for manipulating text and finding files from the command line on macOS. By default, macOS uses BSD-derived UNIX utilities (`grep`, `find`, `sed`, `awk`) which have subtle differences from their GNU/Linux counterparts.

Where applicable, this skill also strongly recommends using modern, high-performance replacements (`ripgrep` / `fd`), but teaches you exactly how to wield the built-in BSD tools.

## Modern High-Performance Alternatives

Always prefer these faster, ergonomic tools over standard BSD tools like grep and find. **They respect `.gitignore` by default and provide sane, colorful outputs.**

- **`ripgrep` (`rg`) instead of `grep`:**
  `rg` is dramatically faster than `grep`, supports `.gitignore` natively, and provides colorful, developer-friendly output.
  - **Basic Search:** `rg 'search_term'` (Recursively search the current directory).
  - **Smart Case:** `rg -S 'term'` (Case-insensitive unless your query contains uppercase letters). Use `-i` for strict case-insensitivity.
  - **Filter by Glob:** `rg -g '*.ts' -g '!*.spec.ts' 'term'` (Search `.ts` files, but exclude `.spec.ts`).
  - **Invert Match:** `rg -v 'ignore_me'` (Print lines that DO NOT match).
  - **Print Filenames Only:** `rg -l 'term'` (List matching files. Use `-0` alongside it for zero-terminating for xargs).
  - **Literal String:** `rg -F 'foo()'` (Treat search as a fixed string, not a regex, avoiding the need to escape parentheses/brackets).
  - **Word Boundaries:** `rg -w 'const'` (Match exactly the word 'const', not 'constant').
  - **Context:** `rg -C 2 'term'` (Show 2 lines of surrounding context. You can also use `-B 2` for before or `-A 2` for after).

  use `rg --help` for more details.

- **`fd` instead of `find`:**
  `fd` is a simple, smart, and blazingly fast alternative to `find`. Like `rg`, it respects `.gitignore` by default.
  - **Basic Search:** `fd 'pattern'` (Find files matching the regex pattern anywhere in the path).
  - **Filter by Extension:** `fd -e py -e txt` (Find files by extension, much faster than `find . -name`).
  - **Filter by Type:** `fd -t d 'docs'` (Find directories). Use `-t f` for files, `-t l` for symlinks.
  - **Hidden & Ignored:** `fd -H` (Include hidden files/directories), `fd -I` (Include ignored files like `node_modules`), `fd -HI` (Search literally everything).
  - **Path Matching:** `fd -p 'src/assets'` (Match against the full path instead of just the filename).
  - **Command Execution:** `fd -e log -x rm` (Execute command individually on each file). Use `-X rm` to run command once passing all files as arguments (like `xargs`).
  - **Absolute Paths:** `fd -a 'pattern'` (Return absolute paths instead of relative ones).

  use `fd --help` for more details.

## Pro-Level Built-in Tools (macOS/BSD)

Use these tools only if `rg` and `fd` are not available. You must be extremely precise and efficient with the standard POSIX tools to avoid crippling terminal slow-downs.

### `grep` - Efficient Searching

Never use `grep | grep -v 'unwanted_dir'` to filter out directories like `node_modules` or `.git`. This is incredibly slow and forces `grep` to read every single junk file before discarding it later. Use native exclusion arguments instead:

```bash
# BAD (Slow, reads binary and ignored files)
grep -R "pattern" . | grep -v "node_modules"

# GOOD (Fast, skips the directory completely at the filesystem level)
grep -R --exclude-dir=node_modules --exclude-dir=.git "pattern" .
```

**Essential `grep` Flags:**
- `-r` / `-R`: Recursive search. (`-R` follows symlinks, `-r` does not).
- `-I` (Capital i): Ignore binary files (crucial for speed and clean terminal output).
- `--exclude="*.min.js"`: Ignore files matching a specific glob.
- `--include="*.dart"`: Only search files matching a specific glob.
- `-l` (Lowercase L): Print only the names of files containing matches, not the matching lines.
- `-Z`: Print a zero-byte (NULL) after each filename (used with `xargs -0`).

**Example: Safely deleting files containing a string**
```bash
grep -rlZ -I --exclude-dir=node_modules "DEPRECATED_API" . | xargs -0 rm
```

### `find` - High-Performance Traversal

Like `grep`, you must prevent `find` from traversing massive, irrelevant directory trees using `-prune`.

```bash
# BAD (Traverses node_modules entirely, then filters output string)
find . -name "*.ts" | grep -v "node_modules"

# GOOD (Skips node_modules entirely)
find . -name "node_modules" -prune -o -name "*.ts" -print
```

**Explanation of Prune:**
`-name "node_modules" -prune` says "if the name is node_modules, prune (stop traversing) it." The `-o` (OR) says "otherwise, if the name ends in `.ts`, print it."

### `sed` - macOS Portability

macOS uses BSD `sed`, heavily diverging from GNU `sed` (Linux) regarding **in-place editing**, which frequently breaks scripts.

**In-place replacement (`-i`):**
On GNU sed, `sed -i 's/a/b/' file` works. On BSD sed (macOS), `-i` requires an explicit backup extension argument, even if it's empty.

```bash
# macOS/BSD (Mandatory empty string argument to -i)
sed -i '' 's/oldName/newName/g' filename.txt

# Create a backup file like filename.txt.bak
sed -i '.bak' 's/oldName/newName/g' filename.txt
```

**Extended Regex (`-E`):**
To avoid escaping every parenthesis and plus sign, use Extended Regular Expressions.
```bash
sed -E 's/(foo|bar)+/baz/g' file.txt
```

### `awk` - Codebase Analysis

`awk` is a complete programming language tailored for line-by-line data extraction.

**Print specific columns:**
```bash
# Print the 1st and 3rd column of space-separated data
ls -l | awk '{print $1, $3}'
```

**Find duplicate lines in a file without sorting:**
This is a classic pro-level awk one-liner.
```bash
awk '!seen[$0]++' filename.txt
```

**Filtering with Awk:**
```bash
# Print lines where the 3rd column is greater than 100
awk '$3 > 100 {print $0}' data.txt

# Print lines containing "Error" and print their line number and content
awk '/Error/ {print NR, $0}' server.log
```

### `xargs` - Safe Pipelines and Parallelism

`xargs` takes standard input and builds execution commands. Always use `-0` (null-terminated) when dealing with files to prevent catastrophic failures on filenames containing spaces or shell metacharacters.

```bash
# BAD (Will fail and potentially delete wrong files if a filename has a space)
find . -name "*.log" | xargs rm

# GOOD (Safe against spaces and quotes)
find . -name "*.log" -print0 | xargs -0 rm
```

**Parallelism (`-P`):**
If you have a CPU-bound task, run it in parallel.
```bash
# Run 4 curl jobs in parallel
cat urls.txt | xargs -n 1 -P 4 curl -O
```

### macOS Specific CLI Tools

- **`mdfind`**: Command-line interface to macOS Spotlight. Insanely fast for finding files globally by name or content without crawling the disk.
  - `mdfind -name "project_spec"`
  - `mdfind "kMDItemTextContent == 'TODO: Refactor'"`
- **`pbcopy` / `pbpaste`**: Directly pipes stdin to the macOS clipboard, or prints the clipboard.
  - `cat ssh_key.pub | pbcopy`
  - `pbpaste > new_file.txt`
- **`open`**: Opens a file, directory, or URL using the default macOS GUI application.
  - `open .` (Opens Finder in current directory)
  - `open -a "Google Chrome" index.html` (Forces using Chrome)
