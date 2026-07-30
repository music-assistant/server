// Sendspin Protocol Types and Interfaces
export var MessageType;
(function (MessageType) {
    MessageType["CLIENT_HELLO"] = "client/hello";
    MessageType["SERVER_HELLO"] = "server/hello";
    MessageType["CLIENT_TIME"] = "client/time";
    MessageType["SERVER_TIME"] = "server/time";
    MessageType["CLIENT_STATE"] = "client/state";
    MessageType["SERVER_STATE"] = "server/state";
    MessageType["CLIENT_COMMAND"] = "client/command";
    MessageType["CLIENT_GOODBYE"] = "client/goodbye";
    MessageType["SERVER_COMMAND"] = "server/command";
    MessageType["STREAM_START"] = "stream/start";
    MessageType["STREAM_CLEAR"] = "stream/clear";
    MessageType["STREAM_REQUEST_FORMAT"] = "stream/request-format";
    MessageType["STREAM_END"] = "stream/end";
    MessageType["GROUP_UPDATE"] = "group/update";
})(MessageType || (MessageType = {}));
//# sourceMappingURL=types.js.map
