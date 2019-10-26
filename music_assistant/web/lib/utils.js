const isMobile = () => (document.body.clientWidth < 800);
const isInStandaloneMode = () => ('standalone' in window.navigator) && (window.navigator.standalone);

function showPlayMenu (item, context=null) {
    /// make the contextmenu visible
    console.log(context);
    this.$globals.contextmenuitem = item;
    this.$globals.contextmenucontext = context;
    this.$globals.showcontextmenu = !this.$globals.showcontextmenu;
    }

function clickItem (item, context=null) {
    /// triggered when user clicks on mediaitem
    var endpoint = "";
    if (item.media_type == 1)
        endpoint = "/artists/"
    else if (item.media_type == 2)
        endpoint = "/albums/"
    else if (item.media_type == 3 || item.media_type == 5)
    {
        this.showPlayMenu(item, context);
        return;
    }
    else if (item.media_type == 4)
        endpoint = "/playlists/"
    item_id = item.item_id.toString();
    var url = endpoint + item_id;
    router.push({ path: url, query: {provider: item.provider}});
}

String.prototype.formatDuration = function () {
    var sec_num = parseInt(this, 10); // don't forget the second param
    var hours   = Math.floor(sec_num / 3600);
    var minutes = Math.floor((sec_num - (hours * 3600)) / 60);
    var seconds = sec_num - (hours * 3600) - (minutes * 60);

    if (hours   < 10) {hours   = "0"+hours;}
    if (minutes < 10) {minutes = "0"+minutes;}
    if (seconds < 10) {seconds = "0"+seconds;}
    if (hours == '00')
        return minutes+':'+seconds;
    else
        return hours+':'+minutes+':'+seconds;
}
function toggleLibrary (item) {
    /// triggered when user clicks the library (heart) button
    var endpoint = this.$globals.server + "api/" + item.media_type + "/";
    item_id = item.item_id.toString();
    var action = "library_remove"
    if (item.in_library.length == 0)
        action = "library_add"
    var url = endpoint + item_id;
    console.log('loading ' + url);
    axios
        .get(url, { params: { provider: item.provider, action: action }})
        .then(result => {
            data = result.data;
            console.log(data);
            if (action == "/library_remove")
                item.in_library = []
            else
                item.in_library = [provider]
            })
        .catch(error => {
            console.log("error", error);
        });
};


