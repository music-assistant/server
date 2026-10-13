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
    MessageType["CLIENT_INIT"] = "client/init";
    MessageType["SERVER_INIT"] = "server/init";
    MessageType["NOISE_HANDSHAKE"] = "noise/handshake";
    MessageType["SERVER_ACTIVATE"] = "server/activate";
    MessageType["CLIENT_PAIR_PENDING"] = "client/pair-pending";
    MessageType["CLIENT_PAIR_INIT"] = "client/pair-init";
    MessageType["SERVER_PAIR_INIT"] = "server/pair-init";
    MessageType["SERVER_PAIR_AUTH"] = "server/pair-auth";
    MessageType["CLIENT_PAIR_AUTH"] = "client/pair-auth";
    MessageType["SERVER_PAIR_CONFIRM"] = "server/pair-confirm";
    MessageType["CLIENT_PAIR_CONFIRM"] = "client/pair-confirm";
    MessageType["CLIENT_PAIR_FINALIZE"] = "client/pair-finalize";
    MessageType["SERVER_PAIR_FINALIZE"] = "server/pair-finalize";
    MessageType["PAIR_ABORT"] = "pair/abort";
    MessageType["SERVER_UNPAIR"] = "server/unpair";
})(MessageType || (MessageType = {}));
//# sourceMappingURL=types.js.map
