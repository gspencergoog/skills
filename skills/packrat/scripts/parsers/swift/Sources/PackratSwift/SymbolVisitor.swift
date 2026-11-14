import Foundation
import SwiftParser
import SwiftSyntax

public func parseSwiftFile(path: String, source: String) -> FileParseResult {
    let tree = Parser.parse(source: source)
    let converter = SourceLocationConverter(fileName: path, tree: tree)
    let sourceBytes = Array(source.utf8)

    let visitor = SymbolVisitor(
        converter: converter,
        sourceBytes: sourceBytes
    )
    visitor.walk(tree)

    let errorCount = countSyntaxErrors(in: tree)
    let finalized = finalizeSymbols(source: source, symbols: visitor.symbols)
    return FileParseResult(symbols: finalized, syntaxErrorCount: errorCount)
}

func countSyntaxErrors(in tree: SourceFileSyntax) -> Int {
    guard tree.hasError else {
        return 0
    }
    let counter = SyntaxErrorCounter(viewMode: .all)
    counter.walk(tree)
    return max(1, counter.errorCount)
}

func computeMaxNestingDepth(in tree: SourceFileSyntax) -> Int {
    let meter = NestingDepthVisitor(viewMode: .sourceAccurate)
    meter.walk(tree)
    return meter.maxDepth
}

private final class SyntaxErrorCounter: SyntaxVisitor {
    var errorCount = 0

    override func visit(_ node: UnexpectedNodesSyntax) -> SyntaxVisitorContinueKind {
        errorCount += 1
        return .skipChildren
    }

    override func visit(_ token: TokenSyntax) -> SyntaxVisitorContinueKind {
        if token.presence == .missing || token.tokenDiagnostic?.severity == .error {
            errorCount += 1
        }
        return .skipChildren
    }
}

private final class NestingDepthVisitor: SyntaxVisitor {
    var currentDepth = 0
    var maxDepth = 0

    private func push() -> SyntaxVisitorContinueKind {
        currentDepth += 1
        if currentDepth > maxDepth {
            maxDepth = currentDepth
        }
        return .visitChildren
    }

    private func pop() {
        currentDepth -= 1
    }

    override func visit(_ node: TupleExprSyntax) -> SyntaxVisitorContinueKind { push() }
    override func visitPost(_ node: TupleExprSyntax) { pop() }

    override func visit(_ node: IfExprSyntax) -> SyntaxVisitorContinueKind { push() }
    override func visitPost(_ node: IfExprSyntax) { pop() }

    override func visit(_ node: ClosureExprSyntax) -> SyntaxVisitorContinueKind { push() }
    override func visitPost(_ node: ClosureExprSyntax) { pop() }

    override func visit(_ node: CodeBlockSyntax) -> SyntaxVisitorContinueKind { push() }
    override func visitPost(_ node: CodeBlockSyntax) { pop() }

    override func visit(_ node: StructDeclSyntax) -> SyntaxVisitorContinueKind { push() }
    override func visitPost(_ node: StructDeclSyntax) { pop() }

    override func visit(_ node: ClassDeclSyntax) -> SyntaxVisitorContinueKind { push() }
    override func visitPost(_ node: ClassDeclSyntax) { pop() }
}

public func splitLinesLF(_ text: String) -> [String] {
    let rawLines = text.components(separatedBy: "\n")
    return rawLines.map { line in
        if line.hasSuffix("\r") {
            return String(line.dropLast())
        }
        return line
    }
}

public func isGeneratedFile(source: String) -> Bool {
    guard !source.isEmpty else { return false }
    let lines = splitLinesLF(source)
    for line in lines where line.count > 4000 {
        return true
    }
    var lineCount = lines.count
    if source.hasSuffix("\n") && lineCount > 1 {
        lineCount -= 1
    }
    lineCount = max(1, lineCount)
    return source.utf8.count > lineCount * 500
}

public func finalizeSymbols(source: String, symbols: [SymbolRecord]) -> [SymbolRecord] {
    let generated = isGeneratedFile(source: source)
    let cap = generated ? 200 : 1000
    var kept: [SymbolRecord] = []
    kept.reserveCapacity(min(symbols.count, cap))
    for sym in symbols {
        if generated && !sym.parent.isEmpty {
            continue
        }
        kept.append(sym)
    }
    kept.sort { a, b in
        if a.lineStart != b.lineStart { return a.lineStart < b.lineStart }
        if a.lineEnd != b.lineEnd { return a.lineEnd < b.lineEnd }
        if a.qualifiedName != b.qualifiedName { return a.qualifiedName < b.qualifiedName }
        return a.kind < b.kind
    }
    if kept.count > cap {
        return Array(kept.prefix(cap))
    }
    return kept
}

struct EnclosingScope {
    let parentQualifiedName: String
    let memberPrefix: String
}

final class SymbolVisitor: SyntaxVisitor {
    let converter: SourceLocationConverter
    let sourceBytes: [UInt8]
    var symbols: [SymbolRecord] = []
    private var scopeStack: [EnclosingScope] = []

    init(converter: SourceLocationConverter, sourceBytes: [UInt8]) {
        self.converter = converter
        self.sourceBytes = sourceBytes
        super.init(viewMode: .sourceAccurate)
    }

    private var currentParent: String {
        scopeStack.last?.parentQualifiedName ?? ""
    }

    private func qualify(_ name: String) -> String {
        guard let top = scopeStack.last, !top.memberPrefix.isEmpty else {
            return name
        }
        return "\(top.memberPrefix).\(name)"
    }

    private func pushTypeScope(qualifiedName: String, memberPrefix: String? = nil) {
        scopeStack.append(
            EnclosingScope(
                parentQualifiedName: qualifiedName,
                memberPrefix: memberPrefix ?? qualifiedName
            )
        )
    }

    private func popScope() {
        _ = scopeStack.popLast()
    }

    private func lineStart(of node: some SyntaxProtocol) -> Int {
        let loc = converter.location(for: node.positionAfterSkippingLeadingTrivia)
        return max(1, loc.line)
    }

    private func lineEnd(of node: some SyntaxProtocol, minLine: Int) -> Int {
        let loc = converter.location(for: node.endPositionBeforeTrailingTrivia)
        return max(minLine, loc.line)
    }

    private func sliceCollapsed(from startPos: AbsolutePosition, to endPos: AbsolutePosition, fallback: String) -> String {
        let start = max(0, min(sourceBytes.count, startPos.utf8Offset))
        let end = max(start, min(sourceBytes.count, endPos.utf8Offset))
        guard end > start else {
            return String(fallback.prefix(500))
        }
        let raw = String(decoding: sourceBytes[start..<end], as: UTF8.self)
        let collapsed = collapseWhitespace(raw)
        if collapsed.isEmpty {
            return String(fallback.prefix(500))
        }
        return String(collapsed.prefix(500))
    }

    private func headerSignature(
        of node: some SyntaxProtocol,
        beforeBrace leftBrace: TokenSyntax?,
        fallback: String
    ) -> String {
        let startPos = node.positionAfterSkippingLeadingTrivia
        let endPos = leftBrace?.position ?? node.endPositionBeforeTrailingTrivia
        return sliceCollapsed(from: startPos, to: endPos, fallback: fallback)
    }

    // MARK: - Type-like declarations

    override func visit(_ node: ClassDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "class",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.memberBlock.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        pushTypeScope(qualifiedName: qname)
        return .visitChildren
    }

    override func visitPost(_ node: ClassDeclSyntax) {
        popScope()
    }

    override func visit(_ node: ActorDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "class",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.memberBlock.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        pushTypeScope(qualifiedName: qname)
        return .visitChildren
    }

    override func visitPost(_ node: ActorDeclSyntax) {
        popScope()
    }

    override func visit(_ node: StructDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "struct",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.memberBlock.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        pushTypeScope(qualifiedName: qname)
        return .visitChildren
    }

    override func visitPost(_ node: StructDeclSyntax) {
        popScope()
    }

    override func visit(_ node: EnumDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "enum",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.memberBlock.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        pushTypeScope(qualifiedName: qname)
        return .visitChildren
    }

    override func visitPost(_ node: EnumDeclSyntax) {
        popScope()
    }

    override func visit(_ node: ProtocolDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "protocol",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.memberBlock.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        pushTypeScope(qualifiedName: qname)
        return .visitChildren
    }

    override func visitPost(_ node: ProtocolDeclSyntax) {
        popScope()
    }

    override func visit(_ node: ExtensionDeclSyntax) -> SyntaxVisitorContinueKind {
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        let extName = "extension@L\(lStart)"
        let parent = currentParent
        let extQName = parent.isEmpty ? extName : "\(parent).\(extName)"
        let receiver = collapseWhitespace(node.extendedType.trimmedDescription)
            .replacingOccurrences(of: " . ", with: ".")
        symbols.append(
            SymbolRecord(
                kind: "extension",
                name: extName,
                qualifiedName: extQName,
                parent: parent,
                signature: headerSignature(of: node, beforeBrace: node.memberBlock.leftBrace, fallback: extName),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        pushTypeScope(
            qualifiedName: extQName,
            memberPrefix: receiver.isEmpty ? extQName : receiver
        )
        return .visitChildren
    }

    override func visitPost(_ node: ExtensionDeclSyntax) {
        popScope()
    }

    // MARK: - Leaf and member declarations

    override func visit(_ node: EnumCaseDeclSyntax) -> SyntaxVisitorContinueKind {
        let caseDoc = extractDocstring(from: node.leadingTrivia)
        let vis = mapVisibility(from: node.modifiers)
        let declStart = lineStart(of: node)
        for (idx, elem) in node.elements.enumerated() {
            let name = cleanIdentifier(elem.name.text)
            guard !name.isEmpty else { continue }
            let qname = qualify(name)
            let elemDoc = extractDocstring(from: elem.leadingTrivia)
            let doc = !elemDoc.isEmpty ? elemDoc : (idx == 0 ? caseDoc : "")
            let lStart = idx == 0 ? declStart : lineStart(of: elem)
            let lEnd = lineEnd(of: elem, minLine: lStart)
            let sig = String(collapseWhitespace("case \(elem.trimmedDescription)").prefix(500))
            symbols.append(
                SymbolRecord(
                    kind: "enum_value",
                    name: name,
                    qualifiedName: qname,
                    parent: currentParent,
                    signature: sig.isEmpty ? name : sig,
                    docstring: doc,
                    lineStart: lStart,
                    lineEnd: lEnd,
                    visibility: vis
                )
            )
        }
        return .skipChildren
    }

    override func visit(_ node: TypeAliasDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        guard !name.isEmpty else { return .skipChildren }
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "typedef",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: nil, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        return .skipChildren
    }

    override func visit(_ node: FunctionDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = cleanIdentifier(node.name.text)
        guard !name.isEmpty else { return .skipChildren }
        let kind = scopeStack.isEmpty ? "function" : "method"
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: kind,
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.body?.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        return .skipChildren
    }

    override func visit(_ node: InitializerDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = "init"
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "constructor",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.body?.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        return .skipChildren
    }

    override func visit(_ node: DeinitializerDeclSyntax) -> SyntaxVisitorContinueKind {
        return .skipChildren
    }

    override func visit(_ node: SubscriptDeclSyntax) -> SyntaxVisitorContinueKind {
        let name = "subscript"
        let qname = qualify(name)
        let lStart = lineStart(of: node)
        let lEnd = lineEnd(of: node, minLine: lStart)
        symbols.append(
            SymbolRecord(
                kind: "method",
                name: name,
                qualifiedName: qname,
                parent: currentParent,
                signature: headerSignature(of: node, beforeBrace: node.accessorBlock?.leftBrace, fallback: name),
                docstring: extractDocstring(from: node.leadingTrivia),
                lineStart: lStart,
                lineEnd: lEnd,
                visibility: mapVisibility(from: node.modifiers)
            )
        )
        return .skipChildren
    }

    override func visit(_ node: VariableDeclSyntax) -> SyntaxVisitorContinueKind {
        let varDoc = extractDocstring(from: node.leadingTrivia)
        let baseVis = mapVisibility(from: node.modifiers)
        let setterModVis = mapSetterVisibility(from: node.modifiers) ?? baseVis
        let declStart = lineStart(of: node)
        let declEnd = lineEnd(of: node, minLine: declStart)
        let storedKind = scopeStack.isEmpty ? "variable" : "field"

        for (bIdx, binding) in node.bindings.enumerated() {
            processPatternBinding(
                binding,
                index: bIdx,
                node: node,
                varDoc: varDoc,
                baseVis: baseVis,
                setterModVis: setterModVis,
                declStart: declStart,
                declEnd: declEnd,
                storedKind: storedKind
            )
        }
        return .skipChildren
    }

    private func processPatternBinding(
        _ binding: PatternBindingSyntax,
        index bIdx: Int,
        node: VariableDeclSyntax,
        varDoc: String,
        baseVis: String,
        setterModVis: String,
        declStart: Int,
        declEnd: Int,
        storedKind: String
    ) {
        let names = extractPatternNames(from: binding.pattern)
        guard !names.isEmpty else { return }
        let bindDoc = extractDocstring(from: binding.leadingTrivia)
        let effectiveDoc = !bindDoc.isEmpty ? bindDoc : (bIdx == 0 ? varDoc : "")

        guard let accessorBlock = binding.accessorBlock else {
            appendStoredBindingSymbols(
                names: names,
                binding: binding,
                index: bIdx,
                node: node,
                kind: storedKind,
                docstring: effectiveDoc,
                visibility: baseVis,
                declStart: declStart,
                declEnd: declEnd
            )
            return
        }

        let baseSig = headerSignature(of: node, beforeBrace: accessorBlock.leftBrace, fallback: names[0])
        switch accessorBlock.accessors {
        case .getter:
            appendNamedSymbols(
                names: names,
                kind: "getter",
                signature: baseSig,
                docstring: effectiveDoc,
                lineStart: declStart,
                lineEnd: declEnd,
                visibility: baseVis
            )
        case .accessors(let accessorList):
            appendExplicitAccessorSymbols(
                accessorList: accessorList,
                names: names,
                storedKind: storedKind,
                baseSig: baseSig,
                effectiveDoc: effectiveDoc,
                baseVis: baseVis,
                setterModVis: setterModVis,
                declStart: declStart,
                declEnd: declEnd
            )
        }
    }

    private func appendStoredBindingSymbols(
        names: [String],
        binding: PatternBindingSyntax,
        index bIdx: Int,
        node: VariableDeclSyntax,
        kind: String,
        docstring: String,
        visibility: String,
        declStart: Int,
        declEnd: Int
    ) {
        let lStart = bIdx == 0 ? declStart : lineStart(of: binding)
        let lEnd = node.bindings.count == 1 ? declEnd : lineEnd(of: binding, minLine: lStart)
        let isSingle = node.bindings.count == 1
        for name in names {
            let sig = isSingle
                ? headerSignature(of: node, beforeBrace: nil, fallback: name)
                : String(collapseWhitespace("\(node.bindingSpecifier.text) \(binding.trimmedDescription)").prefix(500))
            symbols.append(
                SymbolRecord(
                    kind: kind,
                    name: name,
                    qualifiedName: qualify(name),
                    parent: currentParent,
                    signature: sig,
                    docstring: docstring,
                    lineStart: lStart,
                    lineEnd: lEnd,
                    visibility: visibility
                )
            )
        }
    }

    private func appendNamedSymbols(
        names: [String],
        kind: String,
        signature: String,
        docstring: String,
        lineStart: Int,
        lineEnd: Int,
        visibility: String
    ) {
        for name in names {
            symbols.append(
                SymbolRecord(
                    kind: kind,
                    name: name,
                    qualifiedName: qualify(name),
                    parent: currentParent,
                    signature: signature,
                    docstring: docstring,
                    lineStart: lineStart,
                    lineEnd: lineEnd,
                    visibility: visibility
                )
            )
        }
    }

    private func appendExplicitAccessorSymbols(
        accessorList: AccessorDeclListSyntax,
        names: [String],
        storedKind: String,
        baseSig: String,
        effectiveDoc: String,
        baseVis: String,
        setterModVis: String,
        declStart: Int,
        declEnd: Int
    ) {
        let getSetAccessors = accessorList.filter { acc in
            let spec = acc.accessorSpecifier.text
            return spec == "get" || spec == "set"
        }
        if getSetAccessors.isEmpty {
            appendNamedSymbols(
                names: names,
                kind: storedKind,
                signature: baseSig,
                docstring: effectiveDoc,
                lineStart: declStart,
                lineEnd: declEnd,
                visibility: baseVis
            )
            return
        }
        let isSingle = getSetAccessors.count == 1
        for (accIdx, acc) in getSetAccessors.enumerated() {
            appendSingleAccessorSymbols(
                acc,
                index: accIdx,
                isSingle: isSingle,
                names: names,
                baseSig: baseSig,
                effectiveDoc: effectiveDoc,
                baseVis: baseVis,
                setterModVis: setterModVis,
                declStart: declStart,
                declEnd: declEnd
            )
        }
    }

    private func appendSingleAccessorSymbols(
        _ acc: AccessorDeclSyntax,
        index accIdx: Int,
        isSingle: Bool,
        names: [String],
        baseSig: String,
        effectiveDoc: String,
        baseVis: String,
        setterModVis: String,
        declStart: Int,
        declEnd: Int
    ) {
        let isSetter = acc.accessorSpecifier.text == "set"
        let kind = isSetter ? "setter" : "getter"
        let accDoc = extractDocstring(from: acc.leadingTrivia)
        let doc = !accDoc.isEmpty ? accDoc : (accIdx == 0 ? effectiveDoc : "")
        let lStart = (accIdx == 0 && acc.body == nil) ? declStart : lineStart(of: acc)
        let lEnd = isSingle ? declEnd : lineEnd(of: acc, minLine: lStart)
        let accVis = resolveAccessorVisibility(acc, isSetter: isSetter, baseVis: baseVis, setterModVis: setterModVis)
        appendNamedSymbols(
            names: names,
            kind: kind,
            signature: baseSig,
            docstring: doc,
            lineStart: lStart,
            lineEnd: lEnd,
            visibility: accVis
        )
    }

    private func resolveAccessorVisibility(
        _ acc: AccessorDeclSyntax,
        isSetter: Bool,
        baseVis: String,
        setterModVis: String
    ) -> String {
        let fallback = isSetter ? setterModVis : baseVis
        guard let explicitMod = acc.modifier else {
            return fallback
        }
        return mapSingleModifier(explicitMod.name.text) ?? fallback
    }

    override func visit(_ node: CodeBlockSyntax) -> SyntaxVisitorContinueKind {
        return .skipChildren
    }

    override func visit(_ node: ClosureExprSyntax) -> SyntaxVisitorContinueKind {
        return .skipChildren
    }
}

func cleanIdentifier(_ raw: String) -> String {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.count >= 2 && trimmed.hasPrefix("`") && trimmed.hasSuffix("`") {
        return String(trimmed.dropFirst().dropLast())
    }
    return trimmed
}

func extractPatternNames(from pattern: PatternSyntax) -> [String] {
    if let ident = pattern.as(IdentifierPatternSyntax.self) {
        let name = cleanIdentifier(ident.identifier.text)
        return name.isEmpty ? [] : [name]
    }
    if let tuple = pattern.as(TuplePatternSyntax.self) {
        return tuple.elements.flatMap { extractPatternNames(from: $0.pattern) }
    }
    if let valueBinding = pattern.as(ValueBindingPatternSyntax.self) {
        return extractPatternNames(from: valueBinding.pattern)
    }
    return []
}

func collapseWhitespace(_ text: String) -> String {
    let parts = text.split(whereSeparator: { $0.isWhitespace })
    return parts.joined(separator: " ")
}

func mapSingleModifier(_ name: String) -> String? {
    switch name {
    case "private", "fileprivate":
        return "private"
    case "internal":
        return "internal"
    case "public", "open":
        return "public"
    case "package":
        return "package"
    default:
        return nil
    }
}

func mapVisibility(from modifiers: DeclModifierListSyntax) -> String {
    for mod in modifiers where mod.detail == nil {
        if let mapped = mapSingleModifier(mod.name.text) {
            return mapped
        }
    }
    return ""
}

func mapSetterVisibility(from modifiers: DeclModifierListSyntax) -> String? {
    for mod in modifiers {
        if let detail = mod.detail, detail.detail.text == "set" {
            if let mapped = mapSingleModifier(mod.name.text) {
                return mapped
            }
        }
    }
    return nil
}

public func extractDocstring(from trivia: Trivia) -> String {
    var lines: [String] = []
    for piece in trivia.pieces {
        switch piece {
        case .docLineComment(let text):
            lines.append(cleanDocLineComment(text))
        case .docBlockComment(let text):
            lines.append(contentsOf: cleanDocBlockComment(text))
        case .newlines(let count), .carriageReturns(let count), .carriageReturnLineFeeds(let count):
            if count > 1 {
                lines.removeAll()
            }
        case .lineComment, .blockComment:
            lines.removeAll()
        default:
            break
        }
    }
    let joined = lines.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    if joined.count > 4000 {
        return String(joined.prefix(4000))
    }
    return joined
}

private func cleanDocLineComment(_ raw: String) -> String {
    var s = raw
    if s.hasPrefix("///") {
        s.removeFirst(3)
        while s.hasPrefix("/") {
            s.removeFirst()
        }
    }
    if s.hasPrefix(" ") {
        s.removeFirst()
    }
    return s.trimmingCharacters(in: CharacterSet.whitespacesAndNewlines)
}

private func cleanDocBlockComment(_ raw: String) -> [String] {
    var s = raw
    if s.hasPrefix("/**") {
        s.removeFirst(3)
        while s.hasPrefix("*") && s != "*/" {
            s.removeFirst()
        }
    }
    if s.hasSuffix("*/") {
        s.removeLast(2)
    }
    let rawLines = splitLinesLF(s)
    var cleaned: [String] = []
    cleaned.reserveCapacity(rawLines.count)
    for line in rawLines {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("*") {
            trimmed.removeFirst()
            if trimmed.hasPrefix(" ") {
                trimmed.removeFirst()
            }
        }
        cleaned.append(trimmed.trimmingCharacters(in: .whitespaces))
    }
    while let first = cleaned.first, first.isEmpty {
        cleaned.removeFirst()
    }
    while let last = cleaned.last, last.isEmpty {
        cleaned.removeLast()
    }
    return cleaned
}
