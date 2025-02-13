// Run.js
import React, { useState, useRef, useEffect } from "react";
import axios from "axios";
import "./static/run.css";
import AgentFeed from "./components/panels/AgentFeed";
import EnvFeed from "./components/panels/EnvFeed";
import LogPanel from "./components/panels/LogPanel";
import LRunControl from "./components/controls/LRunControl";
import { useImmer } from "use-immer";

const url = ""; // 기본 URL (예: process.env.API_URL 등을 사용할 수 있습니다)
axios.defaults.baseURL = url;

function Run() {
  const [isConnected, setIsConnected] = useState(true);
  const [errorBanner, setErrorBanner] = useState("");

  const runConfigDefault = {
    agent: {
      model: {
        model_name: "gpt4",
      },
    },
    problem_statement: {
      type: "",
      input: "",
    },
    environment: {
      image_name: "",
      script: "",
      repo: {
        type: "",
        input: "",
      },
    },
    extra: {
      test_run: false,
    },
  };
  const [runConfig, setRunConfig] = useImmer(runConfigDefault);

  const [agentFeed, setAgentFeed] = useState([]);
  const [envFeed, setEnvFeed] = useState([]);
  const [highlightedStep, setHighlightedStep] = useState(null);
  const [logs, setLogs] = useState("");
  const [isComputing, setIsComputing] = useState(false);

  const hoverTimeoutRef = useRef(null);

  const agentFeedRef = useRef(null);
  const envFeedRef = useRef(null);
  const logsRef = useRef(null);
  const isLogScrolled = useRef(false);
  const isEnvScrolled = useRef(false);
  const isAgentScrolled = useRef(false);

  const [tabKey, setTabKey] = useState("problem");

  const stillComputingTimeoutRef = useRef(null);

  function scrollToHighlightedStep(highlightedStep, ref) {
    if (highlightedStep && ref.current) {
      console.log("Scrolling to highlighted step", highlightedStep, ref.current);
      const firstStepMessage = ref.current.querySelector(`.step${highlightedStep}`);
      if (firstStepMessage) {
        window.requestAnimationFrame(() => {
          ref.current.scrollTo({
            top: firstStepMessage.offsetTop - ref.current.offsetTop,
            behavior: "smooth",
          });
        });
      }
    }
  }

  function getOtherFeed(feedRef) {
    return feedRef === agentFeedRef ? envFeedRef : agentFeedRef;
  }

  const handleMouseEnter = (item, feedRef) => {
    if (isComputing) return;
    const highlightedStep = item.step;
    if (hoverTimeoutRef.current) {
      clearTimeout(hoverTimeoutRef.current);
    }
    hoverTimeoutRef.current = setTimeout(() => {
      if (!isComputing) {
        setHighlightedStep(highlightedStep);
        scrollToHighlightedStep(highlightedStep, getOtherFeed(feedRef));
      }
    }, 250);
  };

  const handleMouseLeave = () => {
    console.log("Mouse left");
    if (hoverTimeoutRef.current) clearTimeout(hoverTimeoutRef.current);
    setHighlightedStep(null);
  };

  const requeueStopComputeTimeout = () => {
    // 필요 시 타임아웃 재설정 (현재 주석 처리)
    // clearTimeout(stillComputingTimeoutRef.current);
    // setIsComputing(true);
    // stillComputingTimeoutRef.current = setTimeout(() => {
    //   setIsComputing(false);
    //   console.log("No activity for 30s, setting isComputing to false");
    // }, 30000);
  };

  // 폼 제출 시 /run 엔드포인트에 요청을 보냄
  const handleSubmit = async (event) => {
    event.preventDefault();
    setTabKey(null);
    setIsComputing(true);
    setAgentFeed([]);
    setEnvFeed([]);
    setLogs("");
    setHighlightedStep(null);
    setErrorBanner("");
    try {
      await axios.get(`/run`, {
        params: { runConfig: JSON.stringify(runConfig) },
      });
    } catch (error) {
      console.error("Error:", error);
    }
  };

  const handleStop = async () => {
    setIsComputing(false);
    try {
      const response = await axios.get("/stop");
      console.log(response.data);
    } catch (error) {
      console.error("Error stopping:", error);
    }
  };

  const checkScrollPosition = (ref, scrollStateRef, offset = 0) => {
    scrollStateRef.current =
      ref.current.scrollTop + ref.current.clientHeight + offset <
      ref.current.scrollHeight;
  };

  const scrollToBottom = (ref, scrollStateRef) => {
    if (!scrollStateRef.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  };

  const scrollDetectedLog = () => checkScrollPosition(logsRef, isLogScrolled, 58);
  const scrollLog = () => scrollToBottom(logsRef, isLogScrolled);
  const scrollDetectedEnv = () => checkScrollPosition(envFeedRef, isEnvScrolled);
  const scrollEnv = () => scrollToBottom(envFeedRef, isEnvScrolled);
  const scrollDetectedAgent = () => checkScrollPosition(agentFeedRef, isAgentScrolled);
  const scrollAgent = () => scrollToBottom(agentFeedRef, isAgentScrolled);

  // SSE를 통해 서버 업데이트를 받습니다.
  useEffect(() => {
    const eventSource = new EventSource("/stream");

    eventSource.onopen = () => {
      console.log("Connected to server via SSE");
      setIsConnected(true);
      setErrorBanner("");
    };

    eventSource.onerror = (error) => {
      console.error("EventSource error:", error);
      setIsConnected(false);
      setErrorBanner("Connection to flask server lost, please restart it.");
      setIsComputing(false);
      scrollLog(); // 로그 스크롤을 최하단으로
    };

    eventSource.addEventListener("agent", (e) => {
      const data = JSON.parse(e.data);
      requeueStopComputeTimeout();
      setAgentFeed((prevMessages) => [
        ...prevMessages,
        {
          type: data.type,
          message: data.message,
          format: data.format,
          step: data.thought_idx,
        },
      ]);
      if (envFeedRef.current) {
        setTimeout(() => {
          scrollEnv();
        }, 100);
      }
    });

    eventSource.addEventListener("env", (e) => {
      const data = JSON.parse(e.data);
      requeueStopComputeTimeout();
      setEnvFeed((prevMessages) => [
        ...prevMessages,
        {
          message: data.message,
          type: data.type,
          format: data.format,
          step: data.thought_idx,
        },
      ]);
      if (agentFeedRef.current) {
        setTimeout(() => {
          scrollAgent();
        }, 100);
      }
    });

    eventSource.addEventListener("banner", (e) => {
      const data = JSON.parse(e.data);
      setErrorBanner(data.message);
    });

    eventSource.addEventListener("log", (e) => {
      const data = JSON.parse(e.data);
      requeueStopComputeTimeout();
      setLogs((prevLogs) => prevLogs + data.message);
      if (logsRef.current) {
        setTimeout(() => {
          scrollLog();
        }, 100);
      }
    });

    eventSource.addEventListener("finish", (e) => {
      setIsComputing(false);
    });

    return () => {
      eventSource.close();
    };
  }, []);

  function renderErrorMessage() {
    if (errorBanner) {
      return (
        <div className="alert alert-danger" role="alert">
          {errorBanner}
          <br />
          If you think this was a bug, please head over to{" "}
          <a
            href="https://github.com/SWE-agent/SWE-agent/issues"
            target="blank"
            rel="noopener noreferrer"
          >
            our GitHub issue tracker
          </a>
          , check if someone has already reported the issue, and if not, create
          a new issue. Please include the full log, all settings that you
          entered, and a screenshot of this page.
        </div>
      );
    }
    return null;
  }

  return (
    <div className="container-demo">
      {renderErrorMessage()}
      <LRunControl
        isComputing={isComputing}
        isConnected={isConnected}
        handleStop={handleStop}
        handleSubmit={handleSubmit}
        tabKey={tabKey}
        setTabKey={setTabKey}
        runConfig={runConfig}
        setRunConfig={setRunConfig}
        runConfigDefault={runConfigDefault}
      />
      <div id="demo">
        <hr />
        <div className="panels">
          <AgentFeed
            feed={agentFeed}
            highlightedStep={highlightedStep}
            handleMouseEnter={handleMouseEnter}
            handleMouseLeave={handleMouseLeave}
            selfRef={agentFeedRef}
            otherRef={envFeedRef}
          />
          <EnvFeed
            feed={envFeed}
            highlightedStep={highlightedStep}
            handleMouseEnter={handleMouseEnter}
            handleMouseLeave={handleMouseLeave}
            selfRef={envFeedRef}
            otherRef={agentFeedRef}
          />
          <LogPanel logs={logs} logsRef={logsRef} isComputing={isComputing} />
        </div>
      </div>
      <hr />
    </div>
  );
}

export default Run;