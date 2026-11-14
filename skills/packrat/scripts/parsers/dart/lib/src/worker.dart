import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:io';
import 'dart:isolate';
import 'dart:math' as math;

import 'protocol.dart';
import 'symbol_visitor.dart';

/// Default isolate pool concurrency capped at `min(4, processors)`.
int defaultWorkerConcurrency() =>
    math.min(4, math.max(1, Platform.numberOfProcessors));

/// Serializes NDJSON line writes and flushes per message without overlapping
/// concurrent `IOSink.flush()` calls.
final class NdjsonSinkWriter {
  final IOSink _sink;
  Future<void> _chain = Future<void>.value();

  /// Wraps [_sink] for serialized per-line UTF-8 JSON flushing.
  NdjsonSinkWriter(this._sink);

  /// Encodes [payload] as a single NDJSON line and flushes [_sink].
  Future<void> writeJson(Map<String, Object?> payload) {
    final bytes = utf8.encode('${jsonEncode(payload)}\n');
    _chain = _chain.then((_) async {
      _sink.add(bytes);
      await _sink.flush();
    });
    return _chain;
  }

  /// Waits for all queued NDJSON lines to flush.
  Future<void> flushPending() => _chain;
}

final class _ActiveJob {
  final int seq;
  final ParseRequest request;
  final Timer timer;
  final Completer<ParseResponse> completer;

  _ActiveJob({
    required this.seq,
    required this.request,
    required this.timer,
    required this.completer,
  });
}

final class _WorkerSlot {
  final Isolate isolate;
  final ReceivePort receivePort;
  final ReceivePort errorPort;
  final ReceivePort exitPort;
  final SendPort commandPort;
  _ActiveJob? activeJob;
  bool disposed = false;

  _WorkerSlot({
    required this.isolate,
    required this.receivePort,
    required this.errorPort,
    required this.exitPort,
    required this.commandPort,
  });

  void killImmediate() {
    if (disposed) return;
    disposed = true;
    activeJob?.timer.cancel();
    activeJob = null;
    receivePort.close();
    errorPort.close();
    exitPort.close();
    isolate.kill(priority: Isolate.immediate);
  }
}

/// Bounded pool of persistent isolates that enforces per-file `deadline_ms`
/// via `Isolate.kill(priority: Isolate.immediate)`.
final class DartParseWorkerPool {
  /// Maximum concurrent worker isolates in this pool.
  final int concurrency;

  final Queue<_WorkerSlot> _idleSlots = Queue<_WorkerSlot>();
  final Set<_WorkerSlot> _allSlots = <_WorkerSlot>{};
  final Queue<(ParseRequest, Completer<ParseResponse>)> _waiters =
      Queue<(ParseRequest, Completer<ParseResponse>)>();

  int _spawningCount = 0;
  int _nextJobSeq = 1;
  bool _closed = false;

  /// Creates an isolate worker pool capped at [concurrency] isolates.
  DartParseWorkerPool({int? concurrency})
      : concurrency = math.max(1, concurrency ?? defaultWorkerConcurrency());

  /// Pre-warms up to [count] worker isolates in parallel when requested.
  Future<void> start({int? count}) async {
    final target = math.min(concurrency, math.max(0, count ?? concurrency));
    final futures = <Future<void>>[];
    for (var i = _allSlots.length + _spawningCount; i < target; i++) {
      futures.add(_ensureReplacementSlot());
    }
    if (futures.isNotEmpty) {
      await Future.wait(futures);
    }
  }

  /// Parses [request] on an isolate, killing it if `request.deadlineMs` elapses.
  Future<ParseResponse> parse(ParseRequest request) {
    if (_closed) {
      return Future<ParseResponse>.value(
        ParseResponse.failure(id: request.id, error: 'pool-closed'),
      );
    }
    final completer = Completer<ParseResponse>();
    _waiters.addLast((request, completer));
    _pump();
    return completer.future;
  }

  /// Kills all worker isolates and closes receive ports immediately.
  Future<void> close() async {
    _closed = true;
    for (final slot in _allSlots.toList()) {
      slot.killImmediate();
    }
    _allSlots.clear();
    _idleSlots.clear();
    while (_waiters.isNotEmpty) {
      final (req, completer) = _waiters.removeFirst();
      if (!completer.isCompleted) {
        completer.complete(
          ParseResponse.failure(id: req.id, error: 'pool-closed'),
        );
      }
    }
  }

  void _pump() {
    if (_closed) return;
    while (_idleSlots.isNotEmpty && _waiters.isNotEmpty) {
      final slot = _idleSlots.removeFirst();
      if (slot.disposed) continue;
      final (req, completer) = _waiters.removeFirst();
      _dispatchToSlot(slot, req, completer);
    }
    while (_shouldSpawnAnotherSlot()) {
      unawaited(_ensureReplacementSlot());
    }
  }

  bool _shouldSpawnAnotherSlot() {
    if (_closed || _waiters.isEmpty) return false;
    final totalCapacity = _allSlots.length + _spawningCount;
    if (totalCapacity >= concurrency) return false;
    return _idleSlots.length + _spawningCount < _waiters.length;
  }

  void _dispatchToSlot(
    _WorkerSlot slot,
    ParseRequest request,
    Completer<ParseResponse> completer,
  ) {
    final seq = _nextJobSeq++;
    final timer = Timer(Duration(milliseconds: request.deadlineMs), () {
      _handleTimeout(slot, seq);
    });
    slot.activeJob = _ActiveJob(
      seq: seq,
      request: request,
      timer: timer,
      completer: completer,
    );
    slot.commandPort.send(<Object?>[seq, request.path, request.content]);
  }

  void _handleTimeout(_WorkerSlot slot, int seq) {
    final job = slot.activeJob;
    if (job == null || job.seq != seq) return;
    _allSlots.remove(slot);
    slot.killImmediate();
    if (!job.completer.isCompleted) {
      job.completer.complete(
        ParseResponse.failure(id: job.request.id, error: 'timeout'),
      );
    }
    _pump();
  }

  void _handleSlotMessage(_WorkerSlot slot, Object? message) {
    if (slot.disposed || message is! List || message.length < 3) return;
    final seq = message[0];
    final job = slot.activeJob;
    if (job == null || seq != job.seq) return;
    slot.activeJob = null;
    job.timer.cancel();

    final errorText = message[1] as String?;
    final payload = message[2];
    if (!job.completer.isCompleted) {
      if (errorText != null) {
        job.completer.complete(
          ParseResponse.failure(id: job.request.id, error: errorText),
        );
      } else {
        job.completer.complete(_decodeIsolateSuccess(job.request.id, payload));
      }
    }
    if (!_closed && !slot.disposed) {
      _idleSlots.addLast(slot);
      _pump();
    }
  }

  ParseResponse _decodeIsolateSuccess(Object? id, Object? payload) {
    if (payload is! Map) {
      return ParseResponse.failure(id: id, error: 'invalid-worker-reply');
    }
    final rawSymbols = payload['symbols'];
    final syntaxErrors = (payload['syntax_error_count'] as int?) ?? 0;
    final symbols = <SymbolRecord>[];
    if (rawSymbols is List) {
      for (final item in rawSymbols) {
        if (item is Map<String, Object?>) {
          symbols.add(SymbolRecord.fromJson(item));
        } else if (item is Map) {
          symbols.add(SymbolRecord.fromJson(Map<String, Object?>.from(item)));
        }
      }
    }
    return ParseResponse.success(
      id: id,
      symbols: symbols,
      syntaxErrorCount: syntaxErrors,
    );
  }

  void _handleUnexpectedSlotExit(_WorkerSlot slot, String reason) {
    if (slot.disposed) return;
    final job = slot.activeJob;
    _allSlots.remove(slot);
    slot.killImmediate();
    if (job != null && !job.completer.isCompleted) {
      job.completer.complete(
        ParseResponse.failure(id: job.request.id, error: reason),
      );
    }
    _pump();
  }

  Future<void> _ensureReplacementSlot() async {
    if (_closed) return;
    _spawningCount += 1;
    try {
      final slot = await _spawnWarmedSlot();
      if (_closed) {
        slot.killImmediate();
        return;
      }
      _allSlots.add(slot);
      _idleSlots.addLast(slot);
      _pump();
    } finally {
      _spawningCount -= 1;
    }
  }

  Future<_WorkerSlot> _spawnWarmedSlot() async {
    final receivePort = ReceivePort();
    final errorPort = ReceivePort();
    final exitPort = ReceivePort();
    final readyCompleter = Completer<SendPort>();

    final isolate = await Isolate.spawn<SendPort>(
      _workerIsolateEntry,
      receivePort.sendPort,
      onError: errorPort.sendPort,
      onExit: exitPort.sendPort,
      errorsAreFatal: true,
    );

    _WorkerSlot? slot;
    receivePort.listen((Object? msg) {
      if (!readyCompleter.isCompleted && msg is SendPort) {
        readyCompleter.complete(msg);
        return;
      }
      if (slot != null) {
        _handleSlotMessage(slot, msg);
      }
    });
    errorPort.listen((Object? _) {
      if (!readyCompleter.isCompleted) {
        readyCompleter
            .completeError(StateError('worker isolate startup failed'));
      } else if (slot != null) {
        _handleUnexpectedSlotExit(slot, 'worker-error');
      }
    });
    exitPort.listen((Object? _) {
      if (!readyCompleter.isCompleted) {
        readyCompleter.completeError(StateError('worker isolate exited early'));
      } else if (slot != null) {
        _handleUnexpectedSlotExit(slot, 'worker-exit');
      }
    });

    final commandPort = await readyCompleter.future;
    final created = _WorkerSlot(
      isolate: isolate,
      receivePort: receivePort,
      errorPort: errorPort,
      exitPort: exitPort,
      commandPort: commandPort,
    );
    slot = created;
    return created;
  }
}

void _workerIsolateEntry(SendPort hostPort) {
  // Warm up scanner and parser JIT paths before accepting deadline-bound jobs.
  parseDartSource('class _PackratWarmup {}');

  final commandPort = ReceivePort();
  hostPort.send(commandPort.sendPort);

  commandPort.listen((Object? message) {
    if (message is! List || message.length < 3) return;
    final seq = message[0];
    final path = message[1] as String? ?? '';
    final content = message[2] as String? ?? '';
    try {
      final parsed = parseDartSource(content, path: path);
      final wireSymbols =
          parsed.symbols.map((s) => s.toJson()).toList(growable: false);
      hostPort.send(<Object?>[
        seq,
        null,
        <String, Object?>{
          'symbols': wireSymbols,
          'syntax_error_count': parsed.syntaxErrorCount,
        },
      ]);
    } catch (err) {
      hostPort
          .send(<Object?>[seq, 'parse-exception: ${err.runtimeType}', null]);
    }
  });
}

/// Streams NDJSON parse requests from [input], fans them out across a bounded
/// [DartParseWorkerPool], writes responses to [output], and drains cleanly on
/// EOF.
Future<void> runNdjsonWorker({
  required Stream<List<int>> input,
  required IOSink output,
  int? concurrency,
}) async {
  final writer = NdjsonSinkWriter(output);
  await writer.writeJson(const ReadyBanner().toJson());
  final pool = DartParseWorkerPool(concurrency: concurrency);

  final inFlight = <Future<void>>{};
  final lines = input.transform(utf8.decoder).transform(const LineSplitter());

  await for (final line in lines) {
    if (line.trim().isEmpty) continue;
    final decoded = DecodedRequest.fromJsonLine(line);
    if (decoded.error != null || decoded.request == null) {
      final writeFut = writer.writeJson(
        ParseResponse.failure(
          id: decoded.id,
          error: decoded.error ?? 'invalid-request',
        ).toJson(),
      );
      _trackFuture(inFlight, writeFut);
      continue;
    }
    final jobFut = pool.parse(decoded.request!).then((response) {
      return writer.writeJson(response.toJson());
    });
    _trackFuture(inFlight, jobFut);
  }

  if (inFlight.isNotEmpty) {
    await Future.wait(inFlight.toList());
  }
  await pool.close();
  await writer.flushPending();
}

void _trackFuture(Set<Future<void>> inFlight, Future<void> future) {
  inFlight.add(future);
  future.whenComplete(() => inFlight.remove(future));
}
