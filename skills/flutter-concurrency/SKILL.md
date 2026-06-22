---
name: "flutter-concurrency"
description: "Execute long-running, CPU-bound tasks (like parsing large JSON payloads) in a background isolate in Flutter, handling Web fallbacks and integrating safely with state management while avoiding resource leaks."
---
# Flutter Concurrency and Data Management

## Goal
Implements advanced Flutter data handling, including background JSON serialization using Isolates, asynchronous state management, and platform-aware concurrency to ensure jank-free 60fps+ UI rendering. Assumes a standard Flutter environment (Dart 2.19+) with access to `dart:convert`, `dart:isolate`, and standard state management paradigms.

## Decision Logic
Use the following decision tree to determine the correct serialization and concurrency approach before writing code:

1. **Serialization Strategy:**
   * *Condition:* Is the JSON model simple, flat, and rarely changed?
     * *Action:* Use **Manual Serialization** (`dart:convert`).
   * *Condition:* Is the JSON model complex, nested, or part of a large-scale application?
     * *Action:* Use **Code Generation** (`json_serializable` and `build_runner`).
2. **Concurrency Strategy:**
   * *Condition:* Is the data payload small and parsing takes < 16ms?
     * *Action:* Run on the **Main UI Isolate** using standard `async`/`await`.
   * *Condition:* Is the data payload large (e.g., > 1MB JSON) or computationally expensive?
     * *Action:* Offload to a **Background Isolate** using `Isolate.run()`.
   * *Condition:* Does the background task require continuous, two-way communication over time?
     * *Action:* Implement a **Long-lived Isolate** using `ReceivePort` and `SendPort`.
   * *Condition:* Is the target platform Web?
     * *Action:* Use `compute()` as a fallback, as standard `dart:isolate` threading is not supported on Flutter Web.

## Instructions

### 1. Determine Environment and Payload Context
**STOP AND ASK THE USER:**
* "Are you targeting Flutter Web, Mobile, or Desktop?"
* "What is the expected size and complexity of the JSON payload?"
* "Do you prefer manual JSON serialization or code generation (`json_serializable`)?"

### 2. Implement JSON Serialization Models
Based on the user's preference, implement the data models.

**Option A: Manual Serialization**
```dart
import 'dart:convert';

class User {
  final String name;
  final String email;

  User(this.name, this.email);

  User.fromJson(Map<String, dynamic> json)
      : name = json['name'] as String,
        email = json['email'] as String;

  Map<String, dynamic> toJson() => {'name': name, 'email': email};
}
```

**Option B: Code Generation (`json_serializable`)**
Ensure `json_annotation` is in `dependencies`, and `build_runner` / `json_serializable` are in `dev_dependencies`.
```dart
import 'package:json_annotation/json_annotation.dart';

part 'user.g.dart';

@JsonSerializable(explicitToJson: true)
class User {
  final String name;
  
  @JsonKey(name: 'email_address', defaultValue: 'unknown@example.com')
  final String email;

  User(this.name, this.email);

  factory User.fromJson(Map<String, dynamic> json) => _$UserFromJson(json);
  Map<String, dynamic> toJson() => _$UserToJson(this);
}
```
*Validate-and-Fix:* Instruct the user to run `dart run build_runner build --delete-conflicting-outputs` to generate the `*.g.dart` file.

### 3. Implement Background Parsing (Isolates)
To prevent UI jank, offload heavy JSON parsing to a background isolate.

**Option A: Short-lived Isolate (Dart 2.19+)**
Use `Isolate.run()` for one-off heavy computations, ensuring a `kIsWeb` check for web compatibility.
```dart
import 'dart:convert';
import 'dart:isolate';
import 'package:flutter/foundation.dart'; // For kIsWeb
import 'package:flutter/services.dart';

Future<List<User>> fetchAndParseUsers() async {
  // 1. Load data on the main isolate
  final String jsonString = await rootBundle.loadString('assets/large_users.json');
  
  // 2. Fall back to synchronous parsing on the Web
  if (kIsWeb) {
    final List<dynamic> decoded = jsonDecode(jsonString) as List<dynamic>;
    return decoded
        .map((dynamic item) => User.fromJson(item as Map<String, dynamic>))
        .toList();
  }

  // 3. Spawn an isolate, pass the computation, and await the result
  final List<User> users = await Isolate.run<List<User>>(() {
    // This runs on the background isolate
    final List<dynamic> decoded = jsonDecode(jsonString) as List<dynamic>;
    return decoded
        .map((dynamic item) => User.fromJson(item as Map<String, dynamic>))
        .toList();
  });
  
  return users;
}
```

**Option B: Long-lived Isolate (Continuous Data Stream)**
Wrap long-lived isolate logic in a clean, reusable class (`BackgroundWorker`) that manages ports and ensures they are closed cleanly to prevent resource leaks.
```dart
import 'dart:isolate';

class BackgroundWorker {
  Isolate? _isolate;
  ReceivePort? _mainReceivePort;
  SendPort? _backgroundSendPort;

  /// Starts the background worker isolate.
  Future<void> start() async {
    if (_isolate != null) return;

    _mainReceivePort = ReceivePort();
    _isolate = await Isolate.spawn(_backgroundWorker, _mainReceivePort!.sendPort);

    // The first message from the worker is its SendPort
    _backgroundSendPort = await _mainReceivePort!.first as SendPort;
  }

  /// Sends a request to the background worker to perform work on [url] and returns the result.
  Future<dynamic> performTask(String url) async {
    if (_backgroundSendPort == null) {
      throw StateError('Worker is not started. Call start() first.');
    }

    final responsePort = ReceivePort();
    _backgroundSendPort!.send([url, responsePort.sendPort]);

    try {
      final result = await responsePort.first;
      return result;
    } finally {
      responsePort.close(); // Prevent resource leaks
    }
  }

  /// Stops the worker and releases all resources.
  void stop() {
    _mainReceivePort?.close();
    _isolate?.kill(priority: Isolate.beforeNextEvent);
    _isolate = null;
    _backgroundSendPort = null;
    _mainReceivePort = null;
  }

  /// The entry point for the background isolate.
  /// Must be a top-level or static function.
  static void _backgroundWorker(SendPort mainSendPort) async {
    final workerReceivePort = ReceivePort();
    mainSendPort.send(workerReceivePort.sendPort);

    await for (final message in workerReceivePort) {
      if (message == null) break;
      final String url = message[0] as String;
      final SendPort replyPort = message[1] as SendPort;

      try {
        // Perform heavy work here (e.g., HTTP request and parsing)
        final parsedData = await _heavyNetworkAndParse(url);
        replyPort.send(parsedData);
      } catch (e) {
        replyPort.send(e);
      }
    }
    workerReceivePort.close(); // Prevent resource leaks
  }

  static Future<dynamic> _heavyNetworkAndParse(String url) async {
    // Simulated heavy network and parsing task
    await Future<void>.delayed(const Duration(milliseconds: 500));
    return 'Data from $url';
  }
}
```

### 4. Integrate with UI State Management
Bind the asynchronous isolate computation to the UI using `FutureBuilder` to ensure the main thread remains responsive.

```dart
import 'package:flutter/material.dart';

class UserListScreen extends StatefulWidget {
  const UserListScreen({super.key});

  @override
  State<UserListScreen> createState() => _UserListScreenState();
}

class _UserListScreenState extends State<UserListScreen> {
  late Future<List<User>> _usersFuture;

  @override
  void initState() {
    super.initState();
    _usersFuture = fetchAndParseUsers(); // Calls the Isolate.run method
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Users')),
      body: FutureBuilder<List<User>>(
        future: _usersFuture,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(child: CircularProgressIndicator());
          } else if (snapshot.hasError) {
            return Center(child: Text('Error: ${snapshot.error}'));
          } else if (!snapshot.hasData || snapshot.data!.isEmpty) {
            return const Center(child: Text('No users found.'));
          }

          final users = snapshot.data!;
          return ListView.builder(
            itemCount: users.length,
            itemBuilder: (context, index) {
              return ListTile(
                title: Text(users[index].name),
                subtitle: Text(users[index].email),
              );
            },
          );
        },
      ),
    );
  }
}
```

## Constraints
* **No UI in Isolates:** Never attempt to access `dart:ui`, `rootBundle`, or manipulate Flutter Widgets inside a spawned isolate. Isolates do not share memory with the main thread.
* **Web Platform Limitations:** `dart:isolate` is not supported on Flutter Web. If targeting Web, you must perform a `kIsWeb` check and fall back to synchronous execution (or use `compute()` from `package:flutter/foundation.dart` which internally handles web fallback by running on the main thread) to prevent runtime crashes.
* **Immutable Messages:** When passing data between isolates via `SendPort`, prefer passing immutable objects (like Strings or unmodifiable byte data) to avoid deep-copy performance overhead.
* **State Immutability:** Always treat `Widget` properties as immutable. Use `StatefulWidget` and `setState` (or a state management package) to trigger rebuilds when asynchronous data resolves.
* **Reflection:** Do not use `dart:mirrors` for JSON serialization. Flutter disables runtime reflection to enable aggressive tree-shaking and AOT compilation. Always use manual parsing or code generation.
