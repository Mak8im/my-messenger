self.addEventListener("install", (event) => {
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(self.clients.claim());
});

// Слушаем Push уведомления
self.addEventListener("push", function(event) {
    let data = {
        title: "Новое сообщение",
        body: "У тебя новое сообщение",
        url: "/chat"
    };

    try {
        if (event.data) {
            data = event.data.json();
        }
    } catch (e) {
        console.error("Push parse error:", e);
    }

    const options = {
        body: data.body || "У тебя новое сообщение",
        icon: "/static/icon.svg",
        badge: "/static/icon.svg",
        vibrate: [100, 50, 100],
        data: {
            url: data.url || "/chat"
        }
    };

    event.waitUntil(
        self.registration.showNotification(data.title || "Новое сообщение", options)
    );
});

// Обработка клика по уведомлению
self.addEventListener("notificationclick", function(event) {
    event.notification.close();

    const targetUrl = event.notification.data?.url || "/chat";

    event.waitUntil(
        clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
            // Если вкладка уже открыта - переключаемся на неё и редиректим
            for (const client of clientList) {
                if ("focus" in client) {
                    client.navigate(targetUrl);
                    return client.focus();
                }
            }
            // Если нет - открываем новое окно
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});