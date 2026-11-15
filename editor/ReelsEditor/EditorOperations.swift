import Foundation

enum BrollOperationBuilder {
    static func insert(
        parentID: String, assetID: String, sourceIn: Double, sourceOut: Double,
        at: Double, isPhoto: Bool
    ) -> JSONValue {
        var source: [String: JSONValue] = ["asset": .string(assetID)]
        if isPhoto {
            source["duration"] = .number(max(0.8, min(12, sourceOut - sourceIn)))
        } else {
            source["in"] = .number(max(0, sourceIn))
            source["out"] = .number(max(sourceIn + 0.3, sourceOut))
        }
        source["at"] = .number(max(0, at))
        return .object([
            "op": .string("insert_overlay"),
            "parentId": .string(parentID),
            "collection": .string("cutaways"),
            "value": .object(source)
        ])
    }

    static func replace(
        cutawayID: String, assetID: String, sourceIn: Double, sourceOut: Double, isPhoto: Bool
    ) -> [JSONValue] {
        if isPhoto {
            return [
                .object(["op": .string("set_field"), "id": .string(cutawayID),
                         "path": .string("/asset"), "value": .string(assetID)]),
                .object(["op": .string("set_field"), "id": .string(cutawayID),
                         "path": .string("/duration"),
                         "value": .number(max(0.8, min(12, sourceOut - sourceIn)))])
            ]
        }
        return [
            .object(["op": .string("set_field"), "id": .string(cutawayID),
                     "path": .string("/asset"), "value": .string(assetID)]),
            .object(["op": .string("set_field"), "id": .string(cutawayID),
                     "path": .string("/in"), "value": .number(max(0, sourceIn))]),
            .object(["op": .string("set_field"), "id": .string(cutawayID),
                     "path": .string("/out"), "value": .number(max(sourceIn + 0.3, sourceOut))])
        ]
    }

    static func trim(cutawayID: String, sourceIn: Double, sourceOut: Double, minimumDuration: Double) -> JSONValue {
        .object([
            "op": .string("trim_cutaway"),
            "id": .string(cutawayID),
            "sourceIn": .number(max(0, sourceIn)),
            "sourceOut": .number(max(sourceIn + minimumDuration, sourceOut))
        ])
    }
}
