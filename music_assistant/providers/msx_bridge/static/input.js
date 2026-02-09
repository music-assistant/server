/******************************************************************************/
//Input Interaction Plugin v0.0.20
//(c) 2025 Benjamin Zachey
//Action syntax examples:
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|secret@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|search|en@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|secret|fr|Headline@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|search|de|Headline|http://link.to.image@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|secret|es|Headline|http://link.to.image|Extension@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|search|pt|Headline|http://link.to.image|Extension|Hint text@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|secret|it|Headline|http://link.to.image|Extension|Hint text|Placeholder@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}||tr@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|||Headline@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}||||http://link.to.image@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|||||Extension@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}||||||Hint text@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|||||||Placeholder@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}&offset={OFFSET}&limit={LIMIT}||||||||20@http://msx.benzac.de/interaction/input.html"
//- "content:request:interaction:http://link.to.input.handler?input={INPUT}|||||||||Initial input@http://msx.benzac.de/interaction/input.html"
/******************************************************************************/

/******************************************************************************/
//InputSettings
/******************************************************************************/
var InputSettings = {
    MIN_APP_VERSION: "0.1.123",
    MIN_DECOMPRESS_VERSION: "0.1.155",
    MAX_LENGTH: 80,
    MAX_SECRET_LENGTH: 40,
    DEFAULT_SUBMIT_LENGTH: 1,
    DEFAULT_LANG: "en",
    AUTO_SUBMIT_DELAY: 3000 //ms
};
/******************************************************************************/

/******************************************************************************/
//InputKeys
/******************************************************************************/
var InputKeys = {
    EN: {
        "~": ["~", "|", "accent"],
        "1": ["1", "!", "1"],
        "2": ["2", "@", "2"],
        "3": ["3", "#", "3"],
        "4": ["4", "$", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", "^", "6"],
        "7": ["7", "&", "7"],
        "8": ["8", "*", "8"],
        "9": ["9", "(", "9"],
        "0": ["0", ")", "0"],
        "-": ["-", "_", "dash"],
        "=": ["=", "+", "equal"],
        "&": ["&", "&", null],

        "q": ["q", "Q", "q"],
        "w": ["w", "W", "w"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["y", "Y", "y"],
        "u": ["u", "U", "u"],
        "i": ["i", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["[", "[", "bracket_open"],
        "]": ["]", "]", "bracket_close"],
        "@": ["@", "@", null],

        "a": ["a", "A", "a"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": [";", ":", "semicolon"],
        "'": ["'", "\"", "quote"],
        "?": ["?", "?", null],

        "z": ["z", "Z", "z"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": ["m", "M", "m"],
        ",": [",", "<", "comma"],
        ".": [".", ">", "period"],
        "/": ["/", "?", "slash"],
        ":": [":", ":", null]
    },
    FR: {
        "~": ["²", "²", null],
        "1": ["1", "&", "1"],
        "2": ["2", "é", "2"],
        "3": ["3", "\"", "3"],
        "4": ["4", "'", "4"],
        "5": ["5", "(", "5"],
        "6": ["6", "-", "6"],
        "7": ["7", "è", "7"],
        "8": ["8", "_", "8"],
        "9": ["9", "ç", "9"],
        "0": ["0", "à", "0"],
        "-": ["°", ")", null],
        "=": ["+", "=", null],
        "&": ["*", "µ", null],

        "q": ["a", "A", "a"],
        "w": ["z", "Z", "z"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["y", "Y", "y"],
        "u": ["u", "U", "u"],
        "i": ["i", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["^", "^", null],
        "]": ["$", "£", null],
        "@": ["@", "@", null],

        "a": ["q", "Q", "q"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": ["m", "M", "m"],
        "'": ["ù", "%", null],
        "?": ["[", "]", null],

        "z": ["w", "W", "w"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": [",", "?", null],
        ",": [";", ".", null],
        ".": [":", "/", null],
        "/": ["!", "§", null],
        ":": ["<", ">", null]
    },
    DE: {
        "~": ["^", "°", "backslash"],
        "1": ["1", "!", "1"],
        "2": ["2", "\"", "2"],
        "3": ["3", "§", "3"],
        "4": ["4", "$", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", "&", "6"],
        "7": ["7", "/", "7"],
        "8": ["8", "(", "8"],
        "9": ["9", ")", "9"],
        "0": ["0", "=", "0"],
        "-": ["ß", "?", "bracket_open"],
        "=": ["´", "`", "bracket_close"],
        "&": ["#", "'", "slash"],

        "q": ["q", "Q", "q"],
        "w": ["w", "W", "w"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["z", "Z", "z"],
        "u": ["u", "U", "u"],
        "i": ["i", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["ü", "Ü", "semicolon"],
        "]": ["+", "*", "equal"],
        "@": ["@", "@", null],

        "a": ["a", "A", "a"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": ["ö", "Ö", "accent"],
        "'": ["ä", "Ä", "quote"],
        "?": ["[", "]", null],

        "z": ["y", "Y", "y"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": ["m", "M", "m"],
        ",": [",", ";", "comma"],
        ".": [".", ":", "period"],
        "/": ["-", "_", "dash"],
        ":": ["<", ">", null]
    },
    ES: {
        "~": ["º", "ª", null],
        "1": ["1", "!", "1"],
        "2": ["2", "\"", "2"],
        "3": ["3", "·", "3"],
        "4": ["4", "$", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", "&", "6"],
        "7": ["7", "/", "7"],
        "8": ["8", "(", "8"],
        "9": ["9", ")", "9"],
        "0": ["0", "=", "0"],
        "-": ["'", "?", null],
        "=": ["¡", "¿", null],
        "&": ["ç", "Ç", null],

        "q": ["q", "Q", "q"],
        "w": ["w", "W", "w"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["y", "Y", "y"],
        "u": ["u", "U", "u"],
        "i": ["i", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["`", "^", null],
        "]": ["+", "*", null],
        "@": ["@", "@", null],

        "a": ["a", "A", "a"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": ["ñ", "Ñ", null],
        "'": ["´", "\"", null],
        "?": ["[", "]", null],

        "z": ["z", "Z", "z"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": ["m", "M", "m"],
        ",": [",", ";", null],
        ".": [".", ":", null],
        "/": ["-", "_", null],
        ":": ["<", ">", null]
    },
    PT: {
        "~": ["|", "|", null],
        "1": ["1", "!", "1"],
        "2": ["2", "\"", "2"],
        "3": ["3", "#", "3"],
        "4": ["4", "$", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", "&", "6"],
        "7": ["7", "/", "7"],
        "8": ["8", "(", "8"],
        "9": ["9", ")", "9"],
        "0": ["0", "=", "0"],
        "-": ["'", "?", null],
        "=": ["«", "»", null],
        "&": ["~", "^", null],

        "q": ["q", "Q", "q"],
        "w": ["w", "W", "w"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["y", "Y", "y"],
        "u": ["u", "U", "u"],
        "i": ["i", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["+", "*", null],
        "]": ["´", "`", null],
        "@": ["@", "@", null],

        "a": ["a", "A", "a"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": ["ç", "Ç", null],
        "'": ["º", "ª", null],
        "?": ["[", "]", null],

        "z": ["z", "Z", "z"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": ["m", "M", "m"],
        ",": [",", ";", null],
        ".": [".", ":", null],
        "/": ["-", "_", null],
        ":": ["<", ">", null]
    },
    IT: {
        "~": ["|", "|", null],
        "1": ["1", "!", "1"],
        "2": ["2", "\"", "2"],
        "3": ["3", "£", "3"],
        "4": ["4", "$", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", "&", "6"],
        "7": ["7", "/", "7"],
        "8": ["8", "(", "8"],
        "9": ["9", ")", "9"],
        "0": ["0", "=", "0"],
        "-": ["'", "?", null],
        "=": ["ì", "^", null],
        "&": ["ù", "§", null],

        "q": ["q", "Q", "q"],
        "w": ["w", "W", "w"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["y", "Y", "y"],
        "u": ["u", "U", "u"],
        "i": ["i", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["è", "é", null],
        "]": ["+", "*", null],
        "@": ["@", "@", null],

        "a": ["a", "A", "a"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": ["ò", "ç", null],
        "'": ["à", "°", null],
        "?": ["[", "]", null],

        "z": ["z", "Z", "z"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": ["m", "M", "m"],
        ",": [",", ";", null],
        ".": [".", ":", null],
        "/": ["-", "_", null],
        ":": ["<", ">", null]
    },
    TR: {
        "~": ["\"", "é", null],
        "1": ["1", "!", "1"],
        "2": ["2", "'", "2"],
        "3": ["3", "^", "3"],
        "4": ["4", "+", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", "&", "6"],
        "7": ["7", "/", "7"],
        "8": ["8", "(", "8"],
        "9": ["9", ")", "9"],
        "0": ["0", "=", "0"],
        "-": ["*", "?", null],
        "=": ["-", "_", null],
        "&": [",", ";", null],

        "q": ["q", "Q", "q"],
        "w": ["w", "W", "w"],
        "e": ["e", "E", "e"],
        "r": ["r", "R", "r"],
        "t": ["t", "T", "t"],
        "y": ["y", "Y", "y"],
        "u": ["u", "U", "u"],
        "i": ["ı", "I", "i"],
        "o": ["o", "O", "o"],
        "p": ["p", "P", "p"],
        "[": ["ğ", "Ğ", null],
        "]": ["ü", "Ü", null],
        "@": ["@", "@", null],

        "a": ["a", "A", "a"],
        "s": ["s", "S", "s"],
        "d": ["d", "D", "d"],
        "f": ["f", "F", "f"],
        "g": ["g", "G", "g"],
        "h": ["h", "H", "h"],
        "j": ["j", "J", "j"],
        "k": ["k", "K", "k"],
        "l": ["l", "L", "l"],
        ";": ["ş", "Ş", null],
        "'": ["i", "İ", null],
        "?": ["[", "]", null],

        "z": ["z", "Z", "z"],
        "x": ["x", "X", "x"],
        "c": ["c", "C", "c"],
        "v": ["v", "V", "v"],
        "b": ["b", "B", "b"],
        "n": ["n", "N", "n"],
        "m": ["m", "M", "m"],
        ",": ["ö", "Ö", null],
        ".": ["ç", "Ç", null],
        "/": [".", ":", null],
        ":": ["<", ">", null]
    },
    RU: {
        "~": ["ё", "Ё", "accent"],
        "1": ["1", "!", "1"],
        "2": ["2", "\"", "2"],
        "3": ["3", "№", "3"],
        "4": ["4", ";", "4"],
        "5": ["5", "%", "5"],
        "6": ["6", ":", "6"],
        "7": ["7", "?", "7"],
        "8": ["8", "*", "8"],
        "9": ["9", "(", "9"],
        "0": ["0", ")", "0"],
        "-": ["-", "_", "dash"],
        "=": ["=", "+", "equal"],
        "&": ["&", "/", null],

        "q": ["й", "Й", "q"],
        "w": ["ц", "Ц", "w"],
        "e": ["у", "У", "e"],
        "r": ["к", "К", "r"],
        "t": ["е", "Е", "t"],
        "y": ["н", "Н", "y"],
        "u": ["г", "Г", "u"],
        "i": ["ш", "Ш", "i"],
        "o": ["щ", "Щ", "o"],
        "p": ["з", "З", "p"],
        "[": ["х", "Х", "bracket_open"],
        "]": ["ъ", "Ъ", "bracket_close"],
        "@": ["@", "@", null],

        "a": ["ф", "Ф", "a"],
        "s": ["ы", "Ы", "s"],
        "d": ["в", "В", "d"],
        "f": ["а", "А", "f"],
        "g": ["п", "П", "g"],
        "h": ["р", "Р", "h"],
        "j": ["о", "О", "j"],
        "k": ["л", "Л", "k"],
        "l": ["д", "Д", "l"],
        ";": ["ж", "Ж", "semicolon"],
        "'": ["э", "Э", "quote"],
        "?": ["[", "]", null],

        "z": ["я", "Я", "z"],
        "x": ["ч", "Ч", "x"],
        "c": ["с", "С", "c"],
        "v": ["м", "М", "v"],
        "b": ["и", "И", "b"],
        "n": ["т", "Т", "n"],
        "m": ["ь", "Ь", "m"],
        ",": ["б", "Б", "comma"],
        ".": ["ю", "Ю", "period"],
        "/": [".", ",", "slash"],
        ":": ["<", ">", null]
    }
};
/******************************************************************************/

/******************************************************************************/
//InputHandler
/******************************************************************************/
function InputHandler() {
    var CURSOR_CHAR = "{txt:msx-black-soft:│}";
    var SECRET_CHAR = "{ico:fiber-manual-record}";
    var SPACE_CHAR = " ";
    var SPACE_INDICATOR_CHAR = "{ico:msx-black-soft:space-bar}";
    var SPACE_REGEX = / /g;
    var INVALID_REGEX = /[{}\\]/g;

    var infoData = null;

    var inputUrl = null;
    var inputValue = null;
    var inputType = null;
    var inputCursor = -1;
    var inputCapslock = false;
    var inputShift = false;
    var inputVisible = false;
    var inputLang = InputSettings.DEFAULT_LANG;
    var inputHeadline = null;
    var inputBackground = null;
    var inputExtension = null;
    var inputHint = null;
    var inputPlaceholder = null;
    var inputOffset = 0;
    var inputLimit = 0;
    var inputResult = null;
    var inputResultExtendable = false;

    var cachedHeadline = null;
    var cachedBackground = null;
    var cachedExtension = null;

    var dataService = new TVXDataService();
    var readyService = new TVXBusyService();
    var autoSubmit = new TVXDelay(InputSettings.AUTO_SUBMIT_DELAY);
    var submitLength = InputSettings.DEFAULT_SUBMIT_LENGTH;
    var submittedUrl = null;
    var submittedInput = null;
    var submitting = false;
    var extending = false;

    //--------------------------------------------------------------------------
    //Private functions
    //--------------------------------------------------------------------------
    var getDeviceId = function() {
        return TVXTools.strFullCheck(infoData != null && infoData.info != null ? infoData.info.id : null, null);
    };
    var decodeValue = function(value) {
        return TVXTools.isFullStr(value) && value.indexOf("id:") == 0 ? TVXTools.base64DecodeId(value.substr(3)) : value;
    };
    var encodeValue = function(value) {
        return TVXTools.isFullStr(value) ? "id:" + TVXTools.base64EncodeId(value) : value;
    };
    var secureUrl = function(url) {
        return TVXTools.isSecureContext() ? TVXTools.secureUrl(url) : url;
    };
    var completeError = function(error) {
        if (error != null && TVXTools.isFullStr(error.message)) {
            error = error.message;
        }
        if (TVXTools.isFullStr(error) && error.lastIndexOf(".") != error.length - 1) {
            return error + ".";
        }
        return error;
    };
    var createServiceOptions = function(url) {
        return TVXTools.isFullStr(url) ? {
            withCredentials: url.indexOf("credentials") > 0
        } : null;
    };
    var createInputUrl = function(url, input, lang, offset, limit) {
        return TVXTools.strReplaceMap(url, {
            "{ID}": TVXTools.strToUrlStr(getDeviceId()),
            "{INPUT}": TVXTools.strToUrlStr(input),
            "{LANG}": TVXTools.strToUrlStr(lang),
            "{OFFSET}": offset,
            "{LIMIT}": limit
        });
    };
    var getMaxInputLength = function(type) {
        return type === "secret" ? InputSettings.MAX_SECRET_LENGTH : InputSettings.MAX_LENGTH;
    };
    var isMaxInputLength = function(value, type) {
        return TVXTools.isFullStr(value) && value.length >= getMaxInputLength(type);
    };
    var setupType = function(type) {
        if (TVXTools.isFullStr(type)) {
            var separator = type.indexOf(":");
            if (separator >= 0) {
                submitLength = TVXTools.strToNum(type.substring(separator + 1), InputSettings.DEFAULT_SUBMIT_LENGTH);
                type = type.substring(0, separator);
            }
        }
        return type;
    };
    var getInputKeys = function(lang) {
        if (lang == "fr") {
            return InputKeys.FR;
        } else if (lang == "de") {
            return InputKeys.DE;
        } else if (lang == "es") {
            return InputKeys.ES;
        } else if (lang == "pt") {
            return InputKeys.PT;
        } else if (lang == "it") {
            return InputKeys.IT;
        } else if (lang == "tr") {
            return InputKeys.TR;
        } else if (lang == "ru") {
            return InputKeys.RU;
        }
        return InputKeys.EN;
    };
    var cacheResult = function(data) {
        //Note: Cache some values for a better user experience
        if (data != null) {
            cachedHeadline = data.headline;
            cachedBackground = data.background;
            cachedExtension = data.extension;
        } else {
            clearCache();
        }
    };
    var clearCache = function() {
        cachedHeadline = null;
        cachedBackground = null;
        cachedExtension = null;
    };
    var resetInput = function() {
        inputValue = null;
        inputType = null;
        inputCursor = -1;
        inputCapslock = false;
        inputShift = false;
        inputVisible = false;
        inputLang = InputSettings.DEFAULT_LANG;
        inputHeadline = null;
        inputBackground = null;
        inputExtension = null;
        inputHint = null;
        inputPlaceholder = null;
        inputOffset = 0;
        inputLimit = 0;
        inputResult = null;
        submitLength = InputSettings.DEFAULT_SUBMIT_LENGTH;
        resetExtension();
        clearCache();
        completeSubmit();
    };
    var createInputValue = function(type, placeholder, value, cursor, visible) {
        var label = null;
        if (TVXTools.isFullStr(value)) {
            label = "{col:msx-black}";
            if (type === "secret" && !visible) {
                var length = value.length;
                for (var i = 0; i < length; i++) {
                    if (cursor >= 0 && cursor == i) {
                        label += CURSOR_CHAR;
                    }
                    label += SECRET_CHAR;
                }
                if (cursor < 0) {
                    label += CURSOR_CHAR;
                }
            } else {
                if (cursor >= 0 && cursor < value.length) {
                    label += value.substring(0, cursor).replace(SPACE_REGEX, SPACE_INDICATOR_CHAR) + CURSOR_CHAR + value.substring(cursor).replace(SPACE_REGEX, SPACE_INDICATOR_CHAR);
                } else {
                    if (value.length > 0 && value[value.length - 1] == SPACE_CHAR) {
                        label += value.substring(0, value.length - 1) + SPACE_INDICATOR_CHAR;
                    } else {
                        label += value;
                    }
                    label += CURSOR_CHAR;
                }
            }
        } else {
            label = TVXTools.isFullStr(placeholder) ? "{col:msx-black-soft}" + placeholder : CURSOR_CHAR;
        }
        return {
            id: "input_value",
            type: "space",
            color: "msx-white",
            layout: "1,0,15,1",
            offset: "-1,0,1,0",
            label: label
        };
    };
    var createInputOverlay = function(type, visible, busy, max) {
        var suffix = max ? " {ico:msx-black-soft:warning}" : "";
        if (type === "secret") {
            return {
                id: "input_overlay",
                type: "default",
                color: "transparent",
                layout: "0,0,16,1",
                text: "{ico:msx-black-soft:" + (visible ? "visibility" : "visibility-off") + "}" + suffix,
                alignment: "right",
                centration: "text",
                action: "interaction:commit:message:control:visible"
            };
        } else {
            return  {
                id: "input_overlay",
                type: "space",
                color: "transparent",
                layout: "0,0,16,1",
                text: (busy ? "{ico:msx-black-soft:hourglass-top}" : "") + suffix,
                alignment: "right",
                centration: "text"
            };
        }
    };
    var createInputButton = function(input, key, x, y, ox) {
        return {
            enable: input != null,
            type: "button",
            layout: x + "," + y + ",1,1",
            offset: ox + ",0,0,0",
            label: input,
            key: key,
            action: "interaction:commit:message:input:" + input
        };
    };
    var createInputControl = function(control, key, x, y, w, ox, ow, enable) {
        var type = "default";
        var label = null;
        var color = "msx-glass";
        if (control == "back") {
            label = "{ico:msx-red:backspace}";
        } else if (control == "left") {
            label = "{ico:keyboard-arrow-left}";
        } else if (control == "right") {
            label = "{ico:keyboard-arrow-right}";
        } else if (control == "capslock:on") {
            label = "{ico:keyboard-capslock}";
            color = "msx-green";
            control = "capslock";
        } else if (control == "capslock:off") {
            label = "{ico:msx-green:keyboard-capslock}";
            control = "capslock";
        } else if (control == "tab") {
            label = "{ico:first-page}{ico:last-page}";
        } else if (control == "shift:on") {
            label = "{ico:upgrade}";
            color = "msx-yellow";
            control = "shift";
        } else if (control == "shift:off") {
            label = "{ico:msx-yellow:upgrade}";
            control = "shift";
        } else if (control == "clear") {
            label = "{ico:clear}";
        } else if (control.indexOf("lang:") == 0) {
            label = "{ico:language} " + control.substr(5).toUpperCase();
            control = "lang";
        } else if (control == "space") {
            type = "button";
            label = "{ico:space-bar}";
        } else if (control == "done") {
            label = "{ico:done}";
        } else if (control == "search") {
            label = "{ico:search}";
            control = "done";
        } else if (control == "searching") {
            label = "{ico:hourglass-top}";
            control = "done";
        }
        return {
            enable: enable,
            type: type,
            color: color,
            layout: x + "," + y + "," + w + ",1",
            offset: ox + ",0," + ow + ",0",
            label: label,
            key: key,
            action: "interaction:commit:message:control:" + control
        };
    };
    var createInputHint = function(hint) {
        return {
            display: TVXTools.isFullStr(hint),
            type: "space",
            layout: "0,6,16,1",
            offset: "0,0.04,0,0",
            text: TVXTools.strFullCheck(hint, null)
        };
    };
    var createInputPage = function(type, hint, placeholder, value, cursor, capslock, shift, visible, lang) {
        var keys = getInputKeys(lang);
        var shiftIndex = capslock ? (shift ? 0 : 1) : (shift ? 1 : 0);
        var keyIndex = 2;
        var fullValue = TVXTools.isFullStr(value);
        var submitValue = checkSubmit(value);
        if (TVXTools.isFullStr(hint) && autoSubmit.isBusy()) {
            hint = "...";
        }
        return {
            wrap: true,
            offset: TVXTools.isFullStr(hint) ? null : "0,0,0,0.333",
            items: [
                createInputValue(type, placeholder, value, cursor, visible),
                createInputOverlay(type, visible, autoSubmit.isBusy(), isMaxInputLength(value, type)),

                createInputButton(keys["~"][shiftIndex], keys["~"][keyIndex], 0, 1, 0),
                createInputButton(keys["1"][shiftIndex], keys["1"][keyIndex], 1, 1, 0),
                createInputButton(keys["2"][shiftIndex], keys["2"][keyIndex], 2, 1, 0),
                createInputButton(keys["3"][shiftIndex], keys["3"][keyIndex], 3, 1, 0),
                createInputButton(keys["4"][shiftIndex], keys["4"][keyIndex], 4, 1, 0),
                createInputButton(keys["5"][shiftIndex], keys["5"][keyIndex], 5, 1, 0),
                createInputButton(keys["6"][shiftIndex], keys["6"][keyIndex], 6, 1, 0),
                createInputButton(keys["7"][shiftIndex], keys["7"][keyIndex], 7, 1, 0),
                createInputButton(keys["8"][shiftIndex], keys["8"][keyIndex], 8, 1, 0),
                createInputButton(keys["9"][shiftIndex], keys["9"][keyIndex], 9, 1, 0),
                createInputButton(keys["0"][shiftIndex], keys["0"][keyIndex], 10, 1, 0),
                createInputButton(keys["-"][shiftIndex], keys["-"][keyIndex], 11, 1, 0),
                createInputButton(keys["="][shiftIndex], keys["="][keyIndex], 12, 1, 0),
                createInputButton(keys["&"][shiftIndex], keys["&"][keyIndex], 13, 1, 0),

                createInputButton(keys["q"][shiftIndex], keys["q"][keyIndex], 1, 2, 0.5),
                createInputButton(keys["w"][shiftIndex], keys["w"][keyIndex], 2, 2, 0.5),
                createInputButton(keys["e"][shiftIndex], keys["e"][keyIndex], 3, 2, 0.5),
                createInputButton(keys["r"][shiftIndex], keys["r"][keyIndex], 4, 2, 0.5),
                createInputButton(keys["t"][shiftIndex], keys["t"][keyIndex], 5, 2, 0.5),
                createInputButton(keys["y"][shiftIndex], keys["y"][keyIndex], 6, 2, 0.5),
                createInputButton(keys["u"][shiftIndex], keys["u"][keyIndex], 7, 2, 0.5),
                createInputButton(keys["i"][shiftIndex], keys["i"][keyIndex], 8, 2, 0.5),
                createInputButton(keys["o"][shiftIndex], keys["o"][keyIndex], 9, 2, 0.5),
                createInputButton(keys["p"][shiftIndex], keys["p"][keyIndex], 10, 2, 0.5),
                createInputButton(keys["["][shiftIndex], keys["["][keyIndex], 11, 2, 0.5),
                createInputButton(keys["]"][shiftIndex], keys["]"][keyIndex], 12, 2, 0.5),
                createInputButton(keys["@"][shiftIndex], keys["@"][keyIndex], 13, 2, 0.5),

                createInputButton(keys["a"][shiftIndex], keys["a"][keyIndex], 1, 3, 0.75),
                createInputButton(keys["s"][shiftIndex], keys["s"][keyIndex], 2, 3, 0.75),
                createInputButton(keys["d"][shiftIndex], keys["d"][keyIndex], 3, 3, 0.75),
                createInputButton(keys["f"][shiftIndex], keys["f"][keyIndex], 4, 3, 0.75),
                createInputButton(keys["g"][shiftIndex], keys["g"][keyIndex], 5, 3, 0.75),
                createInputButton(keys["h"][shiftIndex], keys["h"][keyIndex], 6, 3, 0.75),
                createInputButton(keys["j"][shiftIndex], keys["j"][keyIndex], 7, 3, 0.75),
                createInputButton(keys["k"][shiftIndex], keys["k"][keyIndex], 8, 3, 0.75),
                createInputButton(keys["l"][shiftIndex], keys["l"][keyIndex], 9, 3, 0.75),
                createInputButton(keys[";"][shiftIndex], keys[";"][keyIndex], 10, 3, 0.75),
                createInputButton(keys["'"][shiftIndex], keys["'"][keyIndex], 11, 3, 0.75),
                createInputButton(keys["?"][shiftIndex], keys["?"][keyIndex], 12, 3, 0.75),

                createInputButton(keys["z"][shiftIndex], keys["z"][keyIndex], 1, 4, 1),
                createInputButton(keys["x"][shiftIndex], keys["x"][keyIndex], 2, 4, 1),
                createInputButton(keys["c"][shiftIndex], keys["c"][keyIndex], 3, 4, 1),
                createInputButton(keys["v"][shiftIndex], keys["v"][keyIndex], 4, 4, 1),
                createInputButton(keys["b"][shiftIndex], keys["b"][keyIndex], 5, 4, 1),
                createInputButton(keys["n"][shiftIndex], keys["n"][keyIndex], 6, 4, 1),
                createInputButton(keys["m"][shiftIndex], keys["m"][keyIndex], 7, 4, 1),
                createInputButton(keys[","][shiftIndex], keys[","][keyIndex], 8, 4, 1),
                createInputButton(keys["."][shiftIndex], keys["."][keyIndex], 9, 4, 1),
                createInputButton(keys["/"][shiftIndex], keys["/"][keyIndex], 10, 4, 1),
                createInputButton(keys[":"][shiftIndex], keys[":"][keyIndex], 11, 4, 1),

                createInputControl("back", "red|delete", 14, 1, 2, 0, 0, true),
                createInputControl("left", "home", 0, 2, 1, 0, 0.5, fullValue),
                createInputControl("right", "end", 14, 2, 2, 0.5, -0.5, fullValue),
                createInputControl("capslock:" + (capslock ? "on" : "off"), "green|caps_lock", 0, 3, 1, 0, 0.75, true),
                createInputControl("tab", "tab", 13, 3, 3, 0.75, -0.75, fullValue),
                createInputControl("shift:" + (shift ? "on" : "off"), "yellow|shift", 0, 4, 1, 0, 1, true),
                createInputControl("clear", null, 12, 4, 4, 1, -1, true),
                createInputControl("lang:" + lang, null, 0, 5, 3, 0, 1, true),
                createInputControl("space", "space|insert", 3, 5, 9, 1, -1, true),
                createInputControl(type === "search" ? (autoSubmit.isBusy() ? "searching" : "search") : "done", null, 11, 5, 5, 1, -1, submitValue),

                createInputHint(hint)
            ]
        };
    };
    var createInputReference = function() {
        return "request:interaction:" +
                encodeValue(TVXTools.strFullCheck(inputUrl, "")) + "|" +
                TVXTools.strFullCheck(inputType, "") + "|" +
                TVXTools.strFullCheck(inputLang, "") + "|" +
                encodeValue(TVXTools.strFullCheck(inputHeadline, "")) + "|" +
                encodeValue(TVXTools.strFullCheck(inputBackground, "")) + "|" +
                encodeValue(TVXTools.strFullCheck(inputExtension, "")) + "|" +
                encodeValue(TVXTools.strFullCheck(inputHint, "")) + "|" +
                encodeValue(TVXTools.strFullCheck(inputPlaceholder, "")) + "|" +
                inputLimit + "@" + window.location.href;
    };
    var createInputContent = function(type, headline, background, extension, hint, placeholder, value, cursor, capslock, shift, visible, lang, result) {
        return {
            type: "list",
            cache: false,
            compress: true,
            important: true,
            reference: createInputReference(),
            preload: TVXTools.strFullCheck(result != null ? result.preload : null, null),
            headline: result != null && TVXTools.isFullStr(result.headline) ? result.headline : TVXTools.strFullCheck(headline, "Input"),
            background: result != null && TVXTools.isFullStr(result.background) ? result.background : TVXTools.strFullCheck(background, null),
            extension: result != null && TVXTools.isFullStr(result.extension) ? result.extension : TVXTools.strFullCheck(extension, null),
            header: createInputPage(type, TVXTools.strFullCheck(result != null ? result.hint : null, hint), placeholder, value, cursor, capslock, shift, visible, lang),
            template: result != null && result.template != null ? result.template : {},
            items: result != null && result.items != null && result.items.length > 0 ? result.items : [],
            footer: result != null && result.footer != null ? result.footer : null,
            inserts: result != null && result.inserts != null && result.inserts.length > 0 ? result.inserts : null
        };
    };
    var createInputLanguageControl = function(id, label, lang) {
        var selected = id == lang;
        return {
            label: label,
            focus: selected,
            extensionIcon: selected ? "msx-white:radio-button-checked" : "radio-button-unchecked",
            action: selected ? "back" : "[back|interaction:commit:message:lang:" + id + "]"
        };
    };
    var createInputLanguagePanel = function(lang) {
        return {
            headline: "Keyboard Layout",
            template: {
                type: "control",
                layout: "0,0,4,1",
                enumerate: false
            },
            items: [
                createInputLanguageControl("en", "English (EN)", lang),
                createInputLanguageControl("fr", "Français (FR)", lang),
                createInputLanguageControl("de", "Deutsch (DE)", lang),
                createInputLanguageControl("es", "Español (ES)", lang),
                createInputLanguageControl("pt", "Portugués (PT)", lang),
                createInputLanguageControl("it", "Italiano (IT)", lang),
                createInputLanguageControl("tr", "Türkçe (TR)", lang),
                createInputLanguageControl("ru", "Русский (RU)", lang)
            ]
        };
    };
    var reloadInputContent = function() {
        TVXInteractionPlugin.executeAction("reload:content");
    };
    var updateInputContent = function() {
        TVXInteractionPlugin.executeAction("update:content:input_value", createInputValue(inputType, inputPlaceholder, inputValue, inputCursor, inputVisible));
        TVXInteractionPlugin.executeAction("update:content:input_overlay", createInputOverlay(inputType, inputVisible, autoSubmit.isBusy(), isMaxInputLength(inputValue, inputType)));
    };
    var executeHandleCode = function(code) {
        if (code == 1) {
            reloadInputContent();
        } else if (code == 2) {
            updateInputContent();
        } else if (code == 3) {
            if (autoSubmit.isBusy()) {
                autoSubmit.finish();
            } else {
                completeSubmit();
            }
        }
    };
    var createWarning = function(headline, message, action) {
        return {
            type: "pages",
            headline: "Warning",
            pages: [{
                    items: [{
                            type: "default",
                            layout: "0,0,12,6",
                            color: "msx-glass",
                            headline: "{ico:msx-yellow:warning} " + TVXTools.strFullCheck(headline, "Content Not Available"),
                            text: [
                                TVXTools.strFullCheck(message, "Content could not be loaded."),
                                "{br}{br}",
                                action == "reload" ? "Please press {txt:msx-white:OK} to reload." : action == "update" ? "Please update Media Station X and try it again." : ""
                            ],
                            action: action == "reload" ? "[invalidate:content|reload:content]" : null
                        }]
                }]
        };
    };
    var createDecompressWarning = function() {
        return {
            template: {
                enumerate: false,
                compress: false,
                type: "default",
                layout: "0,0,16,8",
                color: "msx-glass"
            },
            items: [{
                    headline: "{ico:msx-yellow:warning} Decompression not supported",
                    text: [
                        "The results cannot be displayed because decompression is not supported.{br}{br}",
                        "Media Station X version {txt:msx-white:" + InputSettings.MIN_DECOMPRESS_VERSION + "} or higher is needed for this feature.{br}{br}",
                        "Please update Media Station X and try it again."
                    ]
                }]
        };
    };
    var validateInputValue = function() {
        if (!TVXTools.isFullStr(inputValue)) {
            inputValue = "";
        }
    };
    var checkSubmit = function(input) {
        return TVXTools.isFullStr(input) && input.length >= submitLength;
    };
    var startSubmit = function(url, input) {
        TVXInteractionPlugin.startLoading();
        submittedUrl = url;
        submittedInput = input;
        submitting = true;
    };
    var processSubmit = function(url, input) {
        return submitting && url == submittedUrl && input == submittedInput;
    };
    var completeSubmit = function() {
        TVXInteractionPlugin.stopLoading();
        submittedUrl = null;
        submittedInput = null;
        submitting = false;
    };
    var setupExtension = function(url, data, limit) {
        resetExtension();
        inputResultExtendable = limit > 0 && TVXTools.isHttpUrl(url) && url.indexOf("{OFFSET}") > 0 && url.indexOf("{LIMIT}") > 0;
        return inputResultExtendable ? completeExtensionData(data, 0, limit) : data;
    };
    var resetExtension = function() {
        inputOffset = 0;
        inputResultExtendable = false;
        extending = false;
    };
    var processExtension = function(url, input, offset, limit) {
        return extending &&
                url == inputUrl &&
                input == inputValue &&
                offset == inputOffset &&
                limit == inputLimit;
    };
    var completeExtensionData = function(data, offset, limit) {
        if (data != null && data.items != null && data.items.length > 0) {
            var items = data.items;
            var extendable = items.length - offset == limit;
            for (var i = 0; i < items.length; i++) {
                if (extendable && i == items.length - 1) {
                    //Replace live object for last item in the list to extend it
                    if (items[i].extended !== true) {
                        items[i].extended = true;
                        items[i].liveOrigin = items[i].live != null ? items[i].live : null;
                        items[i].live = {
                            type: "setup",
                            action: "interaction:commit:message:extend"
                        };
                    }
                } else if (items[i].extended === true) {
                    //Restore live object
                    items[i].extended = false;
                    items[i].live = items[i].liveOrigin;
                    items[i].liveOrigin = null;
                }
            }
        }
        return data;
    };
    var mergeExtensionData = function(data, extension, offset, limit) {
        if (data != null && data.items != null && data.items.length > 0 &&
                extension != null && extension.items != null && extension.items.length > 0) {
            for (var i = 0; i < extension.items.length; i++) {
                data.items.push(extension.items[i]);
            }
        }
        return completeExtensionData(data, offset, limit);
    };
    var handleInputResultData = function(url, input, data, limit) {
        if (data != null) {
            if (TVXTools.isFullStr(data.action)) {
                reloadInputContent();
                TVXInteractionPlugin.executeAction(data.action, data.data);
                return true;
            } else if (data.template != null && data.items != null && data.items.length >= 0) {
                if (data.compress !== true && data.template.decompress !== false) {
                    //Note: Decompress template items to get the expected appearance (this only works from version 0.1.155+)
                    //Note: If compressed content should be displayed, this should be indicated (e.g. by setting the compress property to true) to avoid different appearances in different versions
                    data.template.decompress = true;
                }
                if (data.template.decompress === true && !TVXPluginTools.checkApplication(infoData, InputSettings.MIN_DECOMPRESS_VERSION)) {
                    inputResult = createDecompressWarning();
                    resetExtension();
                    clearCache();
                } else {
                    inputResult = setupExtension(url, data, limit);
                    cacheResult(data);
                }
                reloadInputContent();
                return true;
            }
        }
        return false;
    };
    var submitInput = function(url, input, lang, limit) {
        if (TVXTools.isHttpUrl(url) && TVXTools.isFullStr(input)) {
            TVXInteractionPlugin.executeAction("invalidate:content");
            autoSubmit.stop();
            startSubmit(url, input);
            dataService.loadData("temp:data", createInputUrl(url, input, lang, 0, limit), {
                success: function(entry) {
                    if (processSubmit(url, input)) {
                        if (!handleInputResultData(url, input, entry.data, limit)) {
                            TVXInteractionPlugin.warn("Input result data is invalid.");
                            reloadInputContent();
                        }
                    }
                },
                error: function(entry) {
                    if (processSubmit(url, input)) {
                        TVXInteractionPlugin.error("Input result data could not be loaded. " + completeError(entry.error));
                        reloadInputContent();
                    }
                },
                completed: function() {
                    if (processSubmit(url, input)) {
                        completeSubmit();
                    }
                }
            }, createServiceOptions());
        }
    };
    var extendInputResult = function() {
        if (!extending && inputResultExtendable && inputResult != null && TVXTools.isHttpUrl(inputUrl)) {
            extending = true;
            var currentUrl = inputUrl;
            var currentInput = inputValue;
            var currentOffset = inputOffset;
            var currentLimit = inputLimit;
            dataService.loadData("temp:data", createInputUrl(inputUrl, inputValue, null, inputOffset + inputLimit, inputLimit), {
                success: function(entry) {
                    if (inputResult != null && processExtension(currentUrl, currentInput, currentOffset, currentLimit)) {
                        extending = false;
                        inputOffset += inputLimit;
                        inputResult = mergeExtensionData(inputResult, entry.data, inputOffset, inputLimit);
                        reloadInputContent();
                    }
                },
                error: function(entry) {
                    if (inputResult != null && processExtension(currentUrl, currentInput, currentOffset, currentLimit)) {
                        extending = false;
                        TVXInteractionPlugin.warn("Input extension data could not be loaded. " + completeError(entry.error));
                    }
                }
            }, createServiceOptions());
        }
    };
    var onInputChanged = function() {
        resetExtension();
        inputResult = null;
        if (checkSubmit(inputValue)) {
            if (inputType === "search") {
                completeSubmit();
                autoSubmit.start(function() {
                    submitInput(inputUrl, inputValue, inputLang, inputLimit);
                });
            } else {
                autoSubmit.stop();
            }
        } else {
            clearCache();
            autoSubmit.stop();
        }
    };
    var normalizeInput = function(input) {
        return TVXTools.isFullStr(input) ? TVXTools.strFlatten(input).replace(INVALID_REGEX, "") : null;
    };
    var setupInput = function(input) {
        if (TVXTools.isFullStr(input) && input.length < getMaxInputLength(inputType)) {
            inputShift = false;
            inputValue = input;
            inputCursor = -1;
            onInputChanged();
            return 3;//Complete auto submit
        }
        return 0;//No action
    };
    var handleInput = function(input) {
        if (!submitting || inputType === "search") {
            validateInputValue();
            if (TVXTools.isFullStr(input) && inputValue.length < getMaxInputLength(inputType)) {
                var reloadRequired = inputValue.length == 0 || inputShift || submitting || inputResult != null;
                var preSubmitState = checkSubmit(inputValue);
                inputShift = false;
                if (inputCursor >= 0 && inputCursor < inputValue.length) {
                    inputValue = inputValue.substr(0, inputCursor) + input + inputValue.substr(inputCursor);
                    inputCursor++;
                } else {
                    inputValue += input;
                }
                onInputChanged();
                if (!reloadRequired) {
                    reloadRequired = preSubmitState != checkSubmit(inputValue);
                }
                return reloadRequired ? 1 : 2;//Reload or update input
            }
        }
        return 0;//No action
    };
    var handleControl = function(control) {
        if (!submitting || inputType === "search") {
            validateInputValue();
            if (TVXTools.isFullStr(control)) {
                var reloadRequired = inputValue.length == 0 || inputShift || submitting;
                var preSubmitState = checkSubmit(inputValue);
                if (control != "shift" && control != "lang") {
                    inputShift = false;
                }
                if (control == "back") {
                    if (inputValue.length > 0) {
                        if (!reloadRequired) {
                            reloadRequired = inputResult != null;
                        }
                        if (inputCursor >= 0 && inputCursor < inputValue.length) {
                            if (inputCursor > 0) {
                                inputValue = inputValue.substr(0, inputCursor - 1) + inputValue.substr(inputCursor);
                                inputCursor--;
                                onInputChanged();
                            }
                        } else {
                            inputValue = inputValue.substr(0, inputValue.length - 1);
                            onInputChanged();
                        }
                        if (!reloadRequired) {
                            reloadRequired = inputValue.length == 0 || preSubmitState != checkSubmit(inputValue);
                        }
                    }
                    return reloadRequired ? 1 : 2;//Reload or update input
                } else if (control == "left") {
                    if (inputCursor > 0) {
                        inputCursor--;
                    } else if (inputCursor < 0) {
                        inputCursor = inputValue.length - 1;
                    }
                    return reloadRequired ? 1 : 2;//Reload or update input
                } else if (control == "right") {
                    if (inputCursor >= 0) {
                        inputCursor++;
                        if (inputCursor >= inputValue.length) {
                            inputCursor = -1;
                        }
                    }
                    return reloadRequired ? 1 : 2;//Reload or update input
                } else if (control == "capslock") {
                    inputCapslock = !inputCapslock;
                    return 1;//Reload input
                } else if (control == "tab") {
                    if (inputCursor >= 0) {
                        inputCursor = -1;
                    } else {
                        inputCursor = 0;
                    }
                    return reloadRequired ? 1 : 2;//Reload or update input
                } else if (control == "shift") {
                    inputShift = !inputShift;
                    return 1;//Reload input
                } else if (control == "clear") {
                    inputValue = "";
                    inputCursor = -1;
                    onInputChanged();
                    return 1;//Reload input
                } else if (control == "lang") {
                    TVXInteractionPlugin.showPanel(createInputLanguagePanel(inputLang));
                    return 0;//No action
                } else if (control == "space") {
                    if (inputValue.length > 0 && inputValue.length < getMaxInputLength(inputType)) {
                        if (!reloadRequired) {
                            reloadRequired = inputResult != null;
                        }
                        if (inputCursor >= 0 && inputCursor < inputValue.length) {
                            if (inputCursor > 0 && inputValue[inputCursor - 1] != SPACE_CHAR && inputValue[inputCursor] != SPACE_CHAR) {
                                inputValue = inputValue.substr(0, inputCursor) + SPACE_CHAR + inputValue.substr(inputCursor);
                                inputCursor++;
                                onInputChanged();
                            }
                        } else {
                            if (inputValue[inputValue.length - 1] != SPACE_CHAR) {
                                inputValue += SPACE_CHAR;
                                onInputChanged();
                            }
                        }
                        if (!reloadRequired) {
                            reloadRequired = preSubmitState != checkSubmit(inputValue);
                        }
                    }
                    return reloadRequired ? 1 : 2;//Reload or update input
                } else if (control == "done") {
                    if (checkSubmit(inputValue)) {
                        submitInput(inputUrl, inputValue, inputLang, inputLimit);
                        return 0;//No action
                    }
                    return 1;//Reload input
                } else if (control == "visible") {
                    inputVisible = !inputVisible;
                    return reloadRequired ? 1 : 2;//Reload or update input
                }
            }
        }
        return 0;//No action
    };
    var handleLanguage = function(lang) {
        inputShift = false;
        inputLang = TVXTools.strFullCheck(lang, InputSettings.DEFAULT_LANG);
        if (inputType === "search" && TVXTools.isHttpUrl(inputUrl) && inputUrl.indexOf("{LANG}") > 0) {
            onInputChanged();
        }
        return 1;//Reload input
    };
    //--------------------------------------------------------------------------


    //--------------------------------------------------------------------------
    //Public functions
    //--------------------------------------------------------------------------
    this.init = function() {
        //Placeholder
    };
    this.ready = function() {
        readyService.start();
        TVXInteractionPlugin.requestData("info", function(data) {
            infoData = data;
            readyService.stop();
        });
    };
    this.handleData = function(data) {
        if (TVXTools.isFullStr(data.message)) {
            if (data.message.indexOf("input:") == 0) {
                executeHandleCode(handleInput(data.message.substr(6)));
            } else if (data.message.indexOf("control:") == 0) {
                executeHandleCode(handleControl(data.message.substr(8)));
            } else if (data.message.indexOf("lang:") == 0) {
                executeHandleCode(handleLanguage(data.message.substr(5)));
            } else if (data.message == "extend") {
                extendInputResult();
            } else {
                TVXInteractionPlugin.warn("Unknown interaction message: '" + data.message + "'");
            }
        }
    };
    this.handleRequest = function(dataId, data, callback) {
        if (TVXTools.isFullStr(dataId)) {
            readyService.onReady(function() {
                if (TVXPluginTools.checkApplication(infoData, InputSettings.MIN_APP_VERSION)) {
                    var token = dataId.split("|");
                    var url = secureUrl(decodeValue(token[0]));
                    var inputCode = 0;
                    if (inputUrl !== url) {
                        resetInput();
                        inputUrl = url;
                        inputType = setupType(TVXTools.strFullCheck(token.length > 1 ? token[1] : null, null));
                        inputLang = TVXTools.strFullCheck(token.length > 2 ? token[2] : null, InputSettings.DEFAULT_LANG);
                        inputHeadline = decodeValue(TVXTools.strFullCheck(token.length > 3 ? token[3] : null, null));
                        inputBackground = decodeValue(TVXTools.strFullCheck(token.length > 4 ? token[4] : null, null));
                        inputExtension = decodeValue(TVXTools.strFullCheck(token.length > 5 ? token[5] : null, null));
                        inputHint = decodeValue(TVXTools.strFullCheck(token.length > 6 ? token[6] : null, null));
                        inputPlaceholder = decodeValue(TVXTools.strFullCheck(token.length > 7 ? token[7] : null, null));
                        inputLimit = TVXTools.strToNum(token.length > 8 ? token[8] : null, 0);
                        inputCode = setupInput(normalizeInput(decodeValue(TVXTools.strFullCheck(token.length > 9 ? token[9] : null, null))));
                    }
                    if (TVXTools.isHttpUrl(inputUrl)) {
                        callback(createInputContent(
                                inputType,
                                TVXTools.strFullCheck(cachedHeadline, inputHeadline),
                                TVXTools.strFullCheck(cachedBackground, inputBackground),
                                TVXTools.strFullCheck(cachedExtension, inputExtension),
                                inputHint,
                                inputPlaceholder,
                                inputValue,
                                inputCursor,
                                inputCapslock,
                                inputShift,
                                inputVisible,
                                inputLang,
                                inputResult));
                        executeHandleCode(inputCode);
                    } else {
                        callback({
                            error: "Invalid input URL: '" + inputUrl + "'"
                        });
                    }
                } else {
                    callback(createWarning("Version Not Supported", "Media Station X version {txt:msx-white:" + InputSettings.MIN_APP_VERSION + "} or higher is needed for this plugin.", "update"));
                }
            });
        } else {
            callback({
                error: "Invalid input data ID: '" + dataId + "': ID must be a full string"
            });
        }
    };
    //--------------------------------------------------------------------------
}
/******************************************************************************/

/******************************************************************************/
//Setup
/******************************************************************************/
TVXPluginTools.onReady(function() {
    TVXInteractionPlugin.setupHandler(new InputHandler());
    TVXInteractionPlugin.init();
});
/******************************************************************************/
