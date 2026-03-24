function sendMessage() {
    let input = document.getElementById("messageInput");
    let msg = input.value;

    let div = document.getElementById("messages");
    div.innerHTML += "<p>" + msg + "</p>";

    input.value = "";
}