use axum::{
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use signum_starter::app;
use tower::ServiceExt;

#[tokio::test]
async fn health_returns_ok() {
    let request = Request::get("/health").body(Body::empty()).unwrap();
    let response = app().oneshot(request).await.unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let body = to_bytes(response.into_body(), 16).await.unwrap();
    assert_eq!(&body[..], b"ok");
}

#[tokio::test]
async fn unknown_route_returns_not_found() {
    let request = Request::get("/missing").body(Body::empty()).unwrap();
    let response = app().oneshot(request).await.unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}
